// 受信機の出力を SD カードに保存する（sd_log.h の説明を参照）。
#include "sd_log.h"

#if CSI_SD
#include <Preferences.h>
#include <SD.h>
#include <SPI.h>
#include <errno.h>
#include <freertos/queue.h>
#include <freertos/stream_buffer.h>
#include <freertos/task.h>
#include <rom/crc.h>

#include <atomic>

#include <CsiBoot.h>

// 順番が要る（ff.h の型を diskio.h が使う。sd_diskio.cpp と同じ並び）
extern "C" {
// clang-format off
#include "ff.h"
#include "diskio.h"
#include "diskio_impl.h"
// clang-format on
}

// Arduino の SD ドライバ（libraries/SD/src/sd_diskio.cpp）の関数。ヘッダに宣言が無いのでここで宣言する
// （C++ の関数。sd_diskio.cpp の定義と同じ形）。書き込みだけを包んで登録し直す
DSTATUS ff_sd_initialize(uint8_t pdrv);
DSTATUS ff_sd_status(uint8_t pdrv);
DRESULT ff_sd_read(uint8_t pdrv, uint8_t *buffer, DWORD sector, UINT count);
DRESULT ff_sd_write(uint8_t pdrv, const uint8_t *buffer, DWORD sector, UINT count);
DRESULT ff_sd_ioctl(uint8_t pdrv, uint8_t cmd, void *buff);

namespace sd_log {
namespace {

// ---- ピンと設定（M5Stack Basic） ----------------------------------------------
constexpr int PIN_SCK = 18;
constexpr int PIN_MISO = 19;
constexpr int PIN_MOSI = 23;
constexpr int PIN_CS = 4;
constexpr uint32_t SPI_HZ = 25000000;   // SD の SPI の速さ [Hz]（2026-10-07 の試験で書き込み約 385 KB/s）
constexpr const char *MOUNT = "/sd";
constexpr const char *DIR = "/CSI";
constexpr uint32_t MAX_SESSION = 9999;  // S0001〜S9999

constexpr size_t RING_BYTES = 48 * 1024;  // リングバッファ [byte]（40 KB/s で約 1.2 秒分）
constexpr size_t CHUNK = 4096;            // 1 回に書く量 [byte]
constexpr TickType_t RECV_WAIT = pdMS_TO_TICKS(200);  // 塊がたまるのを待つ上限
constexpr uint32_t FLUSH_MS = 1000;       // 確定の間隔 [ms]
constexpr uint32_t STOP_DRAIN_MS = 3000;  // 停止の時に残りを書き終えるのを待つ上限 [ms]
constexpr uint32_t TASK_STACK = 6144;     // [byte]
constexpr UBaseType_t TASK_PRIORITY = 1;  // loop と同じ（SPI の書き込みの間も loop が回るように）
constexpr BaseType_t TASK_CORE = 1;       // Wi-Fi（コア 0）から離す
constexpr size_t SEND_CHUNK = 4096;       // 転送の塊 [byte]
constexpr uint32_t ACK_TIMEOUT_MS = 5000; // 転送の返事を待つ上限 [ms]
constexpr uint32_t MAX_RESEND = 10;       // 同じ塊を送り直す上限
// 切り替えの印（リングバッファに置く 1 バイト。出力は ASCII の行なので本文には現れない）。
// バイト数で境目を数えると、xStreamBufferReset（待っているタスクがいると失敗する）でずれるので、印を流す
constexpr uint8_t ROTATE_MARK = 0x00;
constexpr size_t ROTATE_HEADER_MAX = 1536;  // 切り替えの見出し [byte]（main.cpp の SD_HEADER_MAX と同じ）
// セッションの開始の印（start が流す 1 バイト）。書き込みのタスクはこの印を読むまで、手元やバッファに残った
// 前のセッションの分を捨てる。2026-10-10: 立て直しの直後に、前のセッションの残り（488 バイト）が新しいファイルに
// 書かれ、数えていない分だけカード上が長くなって sync_lag でもう一度立て直していた（xStreamBufferReset は
// 書き込みのタスクが待っている間は効かず、タスクが既に受け取った塊も消せない）
constexpr uint8_t SESSION_MARK = 0x01;
constexpr TickType_t SESSION_MARK_WAIT = pdMS_TO_TICKS(200);  // 印を置く場所が空くのを待つ上限

// ---- 書き込みの書き直し（timed_write） ----
constexpr uint32_t DISK_RETRY_MAX = 3;          // 1 回の書き込みで書き直す上限
constexpr uint32_t DISK_RETRY_DELAY_MS = 2;     // 書き直す前に待つ時間 [ms]（カードが落ち着くのを待つ）
// 書き直しを続ける時間の上限 [ms]。リングバッファ（約 1.2 秒分）があふれないように、その半分に収める。
// 普段の書き込みは最大 150 ms 程度（2026-10-09 の # sdstat の disk_max_ms）
constexpr uint32_t DISK_RETRY_BUDGET_MS = 600;

// ---- 出来事（NVS） ----
constexpr const char *EVENT_NS = "csi_sdev";  // NVS の名前空間（受信機の設定 "csi" とは分ける）
constexpr const char *EVENT_KEY_RING = "ring";
constexpr const char *EVENT_KEY_SEQ = "seq";
constexpr const char *EVENT_KEY_VER = "ver";
constexpr uint8_t EVENT_VERSION = 1;          // Event の並びの版
constexpr size_t EVENT_QUEUE_LEN = 8;         // NVS に書くまでの待ち行列
constexpr uint32_t EVENTS_PER_SESSION = 8;    // 1 セッションで残す上限（RESUME は数えない）
constexpr uint32_t MS_PER_SEC = 1000;
constexpr uint32_t DUR_MS_MAX = 65535;        // Event::dur_ms の上限 [ms]

SemaphoreHandle_t g_bus = nullptr;        // SPI バスの鍵（液晶と共有）
SemaphoreHandle_t g_append_lock = nullptr;  // 書き込みの要求を置く側どうしの鍵
StreamBufferHandle_t g_ring = nullptr;
File g_file;
volatile bool g_open = false;      // g_file が開いている（鍵の中で読み書きする）
volatile bool g_accept = false;    // append を受け付ける
portMUX_TYPE g_status_mux = portMUX_INITIALIZER_UNLOCKED;
Status g_status = {};
uint8_t g_chunk[CHUNK];
uint8_t g_pdrv = 0xFF;                    // SD のドライブ番号（FatFs）
bool g_ready = false;                     // 書き込みのタスクなどを作った（begin の前半が済んだ）
QueueHandle_t g_events = nullptr;         // NVS に書く前の出来事
int64_t (*g_clock)() = nullptr;           // 時刻 [unix sec]（0 = 不明）
uint32_t g_session_events = 0;            // このセッションで待ち行列に置いた出来事の数（g_status_mux で守る）
char g_rotate_header[ROTATE_HEADER_MAX];  // 次のファイルの見出し（rotate が書き、書き込みのタスクが読む）
size_t g_rotate_header_len = 0;

uint32_t session_number(const char *name);

// 流した開始の印のうち、書き込みのタスクがまだ読んでいない数。0 でない間は、書かずに捨てる
std::atomic<uint32_t> g_marks_pending{0};
std::atomic<uint32_t> g_fault_writes{0};  // わざと失敗させる残りの書き込みの数（SDFAULT。試験用）

// ドライバの書き込みを 1 回試す（失敗の注入が残っていれば、カードに触らずに失敗にする）
DRESULT try_write(BYTE pdrv, const BYTE *buff, uint32_t sector, unsigned count) {
  uint32_t left = g_fault_writes.load();
  while (left > 0) {
    if (g_fault_writes.compare_exchange_weak(left, left - 1)) return RES_ERROR;
  }
  return ff_sd_write(pdrv, buff, sector, count);
}

// 出来事を組み立てて待ち行列に置く（どのタスクからでも。待たない）。
void queue_event(EventKind kind, int16_t err, int8_t card_ok, uint32_t dur_ms, uint32_t sector,
                 uint32_t count) {
  if (g_events == nullptr) return;
  Event ev = {};
  ev.unix_s = g_clock != nullptr ? static_cast<int32_t>(g_clock()) : 0;
  ev.uptime_s = millis() / MS_PER_SEC;
  ev.kind = static_cast<uint8_t>(kind);
  ev.card_ok = card_ok;
  ev.dur_ms = static_cast<uint16_t>(dur_ms < DUR_MS_MAX ? dur_ms : DUR_MS_MAX);
  ev.err = err;
  ev.sector = sector;
  ev.count = static_cast<uint16_t>(count);
  bool take = true;
  portENTER_CRITICAL(&g_status_mux);
  if (g_status.recording) {
    ev.session_s = static_cast<int32_t>((millis() - g_status.started_ms) / MS_PER_SEC);
  } else {
    ev.session_s = -1;
  }
  if (kind != EventKind::RESUME && kind != EventKind::RECOVER) {
    if (g_session_events >= EVENTS_PER_SESSION) take = false;
    else g_session_events++;
  }
  const bool recording = g_status.recording;
  char name[NAME_LEN];
  memcpy(name, g_status.name, NAME_LEN);
  portEXIT_CRITICAL(&g_status_mux);
  if (!take) return;
  ev.session = recording ? static_cast<uint16_t>(session_number(name)) : 0;
  xQueueSend(g_events, &ev, 0);
}

// 待ち行列の出来事を NVS の輪に書く（書き込みのタスクから。SPI バスの鍵の外で呼ぶ）。
void persist_events() {
  if (g_events == nullptr) return;
  Event ev;
  while (xQueueReceive(g_events, &ev, 0) == pdTRUE) {
    Preferences p;
    if (!p.begin(EVENT_NS, false)) return;  // NVS が開けない（出来事は捨てる）
    Event ring[EVENT_SLOTS] = {};
    if (p.getUChar(EVENT_KEY_VER, 0) == EVENT_VERSION) {
      p.getBytes(EVENT_KEY_RING, ring, sizeof(ring));
    }
    const uint32_t seq = (p.getUChar(EVENT_KEY_VER, 0) == EVENT_VERSION ? p.getUInt(EVENT_KEY_SEQ, 0) : 0) + 1;
    ev.seq = seq;
    ring[(seq - 1) % EVENT_SLOTS] = ev;
    p.putBytes(EVENT_KEY_RING, ring, sizeof(ring));
    p.putUInt(EVENT_KEY_SEQ, seq);
    p.putUChar(EVENT_KEY_VER, EVENT_VERSION);
    p.end();
  }
}

// ドライバの書き込みを包む: 時間を測り、失敗したらカードに状態を問い合わせて出来事に残す。
// カードが応答しているのに失敗した（RES_ERROR）ときは、同じセクタに同じデータを書き直す。
// FatFs は書き込みに 1 回でも失敗するとそのファイルを使えなくするため、一瞬の失敗でもファイルが打ち切られていた
// （2026-10-09〜10 の 18.5 時間で 9 回。失敗はどれも 6〜68 ms で返り、直後のカードは応答していた。
// Arduino のドライバは複数セクタの書き込みで、カードの CRC の不一致などの応答を書き直さずに失敗にする）。
// FatFs から呼ばれる（書き込みのタスクが SPI バスの鍵を持っている間）。
DRESULT timed_write(BYTE pdrv, const BYTE *buff, uint32_t sector, unsigned count) {
  const uint32_t t0 = millis();
  DRESULT r = try_write(pdrv, buff, sector, count);
  int8_t card_ok = -1;
  uint32_t retries = 0;
  while (r != RES_OK) {
    card_ok = (ff_sd_status(pdrv) & STA_NOINIT) != 0 ? 0 : 1;
    // 書き直すのは、カードが応答しているときの一般の失敗だけ（応答しない・書き込み禁止は立て直しに任せる）。
    // 待ち続けるとリングバッファがあふれるので、時間の上限も設ける
    if (r != RES_ERROR || card_ok != 1 || retries >= DISK_RETRY_MAX ||
        millis() - t0 >= DISK_RETRY_BUDGET_MS) {
      break;
    }
    vTaskDelay(pdMS_TO_TICKS(DISK_RETRY_DELAY_MS));
    retries++;
    r = try_write(pdrv, buff, sector, count);
  }
  const uint32_t dt = millis() - t0;
  const bool rescued = r == RES_OK && retries > 0;
  bool first_rescue = false;
  portENTER_CRITICAL(&g_status_mux);
  g_status.disk_writes++;
  g_status.disk_retry += retries;
  if (dt > g_status.disk_write_max_ms) g_status.disk_write_max_ms = dt;
  if (rescued) {
    first_rescue = g_status.disk_retry_ok == 0;
    g_status.disk_retry_ok++;
  }
  if (r != RES_OK) {
    g_status.disk_fail++;
    g_status.disk_fail_ms = dt;
    g_status.disk_fail_card_ok = card_ok;
    g_status.disk_fail_sector = sector;
    g_status.disk_fail_count = count;
  }
  portEXIT_CRITICAL(&g_status_mux);
  if (r != RES_OK) {
    queue_event(EventKind::DISK_FAIL, static_cast<int16_t>(r), card_ok, dt, sector, count);
  } else if (first_rescue) {
    // 救えた回は毎回残すと NVS の輪（16 件）を埋めるので、セッションで最初の 1 回だけ
    queue_event(EventKind::DISK_RETRY, static_cast<int16_t>(retries), card_ok, dt, sector, count);
  }
  return r;
}

// 状態の問い合わせを包む（FatFs が書き込み・確定の前に毎回呼ぶ。STA_NOINIT なら FatFs は断る）。
DSTATUS timed_status(BYTE pdrv) {
  const uint32_t t0 = millis();
  const DSTATUS st = ff_sd_status(pdrv);
  const uint32_t dt = millis() - t0;
  const bool failed = (st & STA_NOINIT) != 0;
  bool first = false;
  portENTER_CRITICAL(&g_status_mux);
  if (dt > g_status.status_max_ms) g_status.status_max_ms = dt;
  if (failed) {
    first = g_status.status_fail == 0;
    g_status.status_fail++;
    g_status.status_fail_ms = dt;
  }
  portEXIT_CRITICAL(&g_status_mux);
  // 失敗が続くと毎回呼ばれるので、出来事にするのはセッションで最初の 1 回だけ
  if (failed && first) queue_event(EventKind::STATUS_FAIL, 0, 0, dt, 0, 0);
  return st;
}

// 読み出しを包む（FatFs がクラスタの表などを読むとき）。
DRESULT timed_read(BYTE pdrv, BYTE *buff, uint32_t sector, unsigned count) {
  const uint32_t t0 = millis();
  const DRESULT r = ff_sd_read(pdrv, buff, sector, count);
  const uint32_t dt = millis() - t0;
  if (r != RES_OK) {
    bool first = false;
    portENTER_CRITICAL(&g_status_mux);
    first = g_status.read_fail == 0;
    g_status.read_fail++;
    portEXIT_CRITICAL(&g_status_mux);
    if (first) queue_event(EventKind::READ_FAIL, static_cast<int16_t>(r), -1, dt, sector, count);
  }
  return r;
}

const ff_diskio_impl_t TIMED_IMPL = {
    &ff_sd_initialize, &timed_status, &timed_read, &timed_write, &ff_sd_ioctl,
};

const char *event_kind_name(uint8_t kind) {
  switch (static_cast<EventKind>(kind)) {
    case EventKind::DISK_FAIL: return "disk_fail";
    case EventKind::WRITE_SHORT: return "write_short";
    case EventKind::SYNC_LAG: return "sync_lag";
    case EventKind::RESUME: return "resume";
    case EventKind::STATUS_FAIL: return "status_fail";
    case EventKind::READ_FAIL: return "read_fail";
    case EventKind::RECOVER: return "recover";
    case EventKind::DISK_RETRY: return "disk_retry";
  }
  return "unknown";
}

// err_no は失敗の直後の errno（分からなければ 0）。
void set_error(const char *what, int err_no = 0) {
  const uint32_t now = millis();
  portENTER_CRITICAL(&g_status_mux);
  g_status.errors++;
  strncpy(g_status.last_error, what, ERROR_LEN - 1);
  g_status.last_error[ERROR_LEN - 1] = '\0';
  g_status.last_errno = err_no;
  if (g_status.recording && g_status.first_error_ms == 0) {
    g_status.first_error_ms = now;
    strncpy(g_status.first_error, what, ERROR_LEN - 1);
    g_status.first_error[ERROR_LEN - 1] = '\0';
    g_status.first_errno = err_no;
  }
  portEXIT_CRITICAL(&g_status_mux);
}

void lock_bus() { xSemaphoreTake(g_bus, portMAX_DELAY); }
void unlock_bus() { xSemaphoreGive(g_bus); }

uint64_t measure_free() {
  lock_bus();
  const uint64_t free_b = SD.totalBytes() - SD.usedBytes();
  unlock_bus();
  return free_b;
}

// "S0001.csv" の番号（違う名前は 0）。
uint32_t session_number(const char *name) {
  if (name[0] != 'S' || strlen(name) != 9 || strcmp(name + 5, ".csv") != 0) return 0;
  uint32_t n = 0;
  for (int i = 1; i <= 4; i++) {
    if (name[i] < '0' || name[i] > '9') return 0;
    n = n * 10 + static_cast<uint32_t>(name[i] - '0');
  }
  return n;
}

// 塊を今のファイルに書く（書き込みのタスクから）。頼んだ量より少なく書けたら false。
bool write_out(const uint8_t *data, size_t n) {
  lock_bus();
  // 打ち切った後、または次のセッションの開始の印より前の分（前のセッションの残り）は捨てる。
  // 鍵の中で見る（書き込みのタスクが鍵を待つ間に start が新しいファイルを開くことがある）
  if (!g_open || g_marks_pending.load() > 0) {
    unlock_bus();
    return true;
  }
  const uint32_t t0 = millis();
  errno = 0;
  const size_t w = g_file.write(data, n);
  const int write_errno = errno;
  const uint32_t dt = millis() - t0;
  unlock_bus();
  portENTER_CRITICAL(&g_status_mux);
  g_status.bytes += w;
  g_status.total_bytes += w;
  if (dt > g_status.write_max_ms) g_status.write_max_ms = dt;
  portEXIT_CRITICAL(&g_status_mux);
  if (w == n) return true;
  const bool first_short = g_status.errors_short == 0;
  portENTER_CRITICAL(&g_status_mux);
  g_status.errors_short++;
  portEXIT_CRITICAL(&g_status_mux);
  set_error("write_short", write_errno);
  if (first_short) {  // 失敗は続くので、出来事にするのはセッションで最初の 1 回だけ
    queue_event(EventKind::WRITE_SHORT, static_cast<int16_t>(write_errno), -1, 0, 0, 0);
  }
  return false;
}

// /CSI にある通し番号の最大（SPI バスの鍵を持って呼ぶ）。
uint32_t max_session_on_card() {
  uint32_t max_n = 0;
  File dir = SD.open(DIR);
  if (dir) {
    for (File f = dir.openNextFile(); f; f = dir.openNextFile()) {
      const uint32_t n = session_number(f.name());
      if (n > max_n) max_n = n;
      f.close();
    }
    dir.close();
  }
  return max_n;
}

// 通し番号 n のファイルを新しく開いて g_file にする（SPI バスの鍵を持って呼ぶ）。名前を name に入れる。
bool open_session_file(uint32_t n, char *name) {
  if (n > MAX_SESSION) {
    set_error("too_many_sessions");
    return false;
  }
  snprintf(name, NAME_LEN, "S%04lu.csv", static_cast<unsigned long>(n));
  char path[32];
  snprintf(path, sizeof(path), "%s/%s", DIR, name);
  g_file = SD.open(path, FILE_WRITE);
  if (!g_file) {
    set_error("open_failed");
    return false;
  }
  return true;
}

// 次の通し番号のファイルに切り替える（書き込みのタスクから。印まで書き終えたところで呼ぶ）。
void rotate_file() {
  const uint32_t t0 = millis();
  char prev[NAME_LEN];
  portENTER_CRITICAL(&g_status_mux);
  memcpy(prev, g_status.name, NAME_LEN);
  portEXIT_CRITICAL(&g_status_mux);
  char name[NAME_LEN] = {};
  bool ok = false;
  lock_bus();
  if (g_open) {
    g_file.flush();
    g_file.close();
    g_open = false;
    uint32_t n = session_number(prev) + 1;
    char path[32];
    snprintf(path, sizeof(path), "%s/S%04lu.csv", DIR, static_cast<unsigned long>(n));
    if (SD.exists(path)) n = max_session_on_card() + 1;  // 番号が埋まっていれば（通常は来ない）最大の次
    ok = open_session_file(n, name);
    if (ok) {
      g_file.write(reinterpret_cast<const uint8_t *>(g_rotate_header), g_rotate_header_len);
      g_file.flush();
      g_open = true;
    }
  }
  unlock_bus();
  const uint32_t dt = millis() - t0;
  portENTER_CRITICAL(&g_status_mux);
  g_status.rotate_pending = false;
  if (dt > g_status.rotate_max_ms) g_status.rotate_max_ms = dt;
  if (ok) {
    memcpy(g_status.name, name, NAME_LEN);
    g_status.bytes = g_rotate_header_len;
    g_status.synced_bytes = g_rotate_header_len;
    g_status.total_bytes += g_rotate_header_len;
    g_status.part++;
    g_status.file_started_ms = millis();
  }
  portEXIT_CRITICAL(&g_status_mux);
  // 開けなければ failed() になり、main が立て直す（新しいセッションを開く）
}

// 書き込みのタスク: リングバッファから塊を取り出して書き、1 秒ごとに確定する。切り替えの印があれば、
// 印の前までを今のファイルに、後ろを次のファイルに書く。
void writer_task(void *arg) {
  (void)arg;
  uint32_t last_flush = millis();
  bool dirty = false;
  for (;;) {
    // 出来事を NVS へ（鍵の外で。NVS はフラッシュで SD とは別のバス）。書き込みに失敗すると下の continue で
    // ループの残りを飛ばすので、先頭で呼ぶ（2026-10-08: 末尾に置いていて、失敗中の出来事が残らなかった）
    persist_events();
    const size_t n = xStreamBufferReceive(g_ring, g_chunk, CHUNK, RECV_WAIT);
    bool short_write = false;
    size_t off = 0;
    while (off < n) {
      const uint8_t *seg = g_chunk + off;
      size_t len = 0;  // 次の印（切り替え・開始）までの長さ
      while (len < n - off && seg[len] != ROTATE_MARK && seg[len] != SESSION_MARK) len++;
      if (len > 0) {
        dirty = true;
        if (!write_out(seg, len)) {
          short_write = true;
          break;
        }
      }
      if (len == n - off) break;  // 印は無かった
      const uint8_t mark = seg[len];
      off += len + 1;
      if (mark == SESSION_MARK) {  // ここから後ろが新しいセッションの分
        g_marks_pending.fetch_sub(1);
        continue;
      }
      if (g_marks_pending.load() > 0) continue;  // 前のセッションの切り替えの印は無視する
      portENTER_CRITICAL(&g_status_mux);
      const bool pending = g_status.rotate_pending;
      portEXIT_CRITICAL(&g_status_mux);
      if (pending) {  // 前のセッションの残りの印（打ち切りの後など）は無視する
        rotate_file();
        last_flush = millis();  // 前のファイルは閉じるときに、次は見出しの後に確定した
        dirty = false;
      }
    }
    if (short_write) continue;
    if (dirty && millis() - last_flush >= FLUSH_MS) {
      lock_bus();
      bool lagging = false;
      int lag_errno = 0;
      if (g_open) {
        const uint32_t t0 = millis();
        errno = 0;
        g_file.flush();
        const uint32_t dt = millis() - t0;  // 確定だけの時間（これまでの flush_max_ms と比べられるように）
        // カード上の長さ（stat。確定した分だけがディレクトリに載る）。書いたつもりの量と比べる
        const size_t on_card = g_file.size();
        lag_errno = errno;
        portENTER_CRITICAL(&g_status_mux);
        if (dt > g_status.flush_max_ms) g_status.flush_max_ms = dt;
        g_status.synced_bytes = on_card;
        lagging = on_card != g_status.bytes;
        if (lagging) g_status.sync_lag++;
        portEXIT_CRITICAL(&g_status_mux);
      }
      unlock_bus();
      if (lagging) {
        set_error("sync_lag", lag_errno);
        if (g_status.sync_lag == 1) {  // ずれは毎秒続くので、最初の 1 回だけ出来事にする
          queue_event(EventKind::SYNC_LAG, static_cast<int16_t>(lag_errno), -1, 0, 0, 0);
        }
      }
      last_flush = millis();
      dirty = false;
    }
  }
}

// カードを取り付け、ドライバを包み直す（SPI バスの鍵を持って呼ぶ）。SD.begin は既に付いていると何もしないので、
// 付け直すときは先に SD.end() を呼ぶ（SD.end はドライバの登録も消す）。
bool mount_card() {
  // SD.begin が使うドライブ番号は「次に空いている番号」。先に同じ方法で取っておき、書き込みなどを包み直す
  BYTE pdrv = 0xFF;
  if (ff_diskio_get_drive(&pdrv) != ESP_OK) pdrv = 0xFF;
  const bool ok = SD.begin(PIN_CS, SPI, SPI_HZ, MOUNT);
  bool hooked = false;
  if (ok && pdrv != 0xFF) {
    ff_diskio_register(pdrv, &TIMED_IMPL);
    g_pdrv = pdrv;
    hooked = true;
  }
  if (ok && !SD.exists(DIR)) SD.mkdir(DIR);
  portENTER_CRITICAL(&g_status_mux);
  g_status.disk_hooked = hooked;
  g_status.mounted = ok;
  portEXIT_CRITICAL(&g_status_mux);
  return ok;
}

}  // namespace

bool begin(SemaphoreHandle_t bus_lock) {
  g_bus = bus_lock;
  g_append_lock = xSemaphoreCreateMutex();
  g_ring = xStreamBufferCreate(RING_BYTES, 1);
  g_events = xQueueCreate(EVENT_QUEUE_LEN, sizeof(Event));
  if (g_bus == nullptr || g_append_lock == nullptr || g_ring == nullptr || g_events == nullptr) {
    set_error("no_memory");
    return false;
  }
  // 書き込みのタスクはカードが無くても先に作る（後から差されたカードを remount で付けられるように・2026-10-08）
  if (xTaskCreatePinnedToCore(writer_task, "sd_writer", TASK_STACK, nullptr, TASK_PRIORITY,
                              nullptr, TASK_CORE) != pdPASS) {
    set_error("no_task");
    return false;
  }
  g_ready = true;
  lock_bus();
  SPI.begin(PIN_SCK, PIN_MISO, PIN_MOSI, -1);
  const bool ok = mount_card();
  unlock_bus();
  if (!ok) {
    set_error("no_card");
    return false;
  }
  g_status.free_bytes = measure_free();
  return true;
}

bool remount() {
  if (!g_ready || g_accept) return false;
  lock_bus();
  if (g_open) {  // 念のため（abort の後なら閉じてある）
    g_file.close();
    g_open = false;
  }
  SD.end();
  const bool ok = mount_card();
  unlock_bus();
  if (!ok) {
    set_error("no_card");
    return false;
  }
  g_status.free_bytes = measure_free();
  return true;
}

void abort() {
  if (!g_accept) return;
  g_accept = false;  // これ以上は置かない。バッファに残った分は、書き込みのタスクが g_open を見て捨てる
  lock_bus();
  if (g_open) {
    g_file.close();  // 失敗中なら確定できないが、ファイルの番号は返される
    g_open = false;
  }
  unlock_bus();
  portENTER_CRITICAL(&g_status_mux);
  g_status.recording = false;
  portEXIT_CRITICAL(&g_status_mux);
}

bool failed() {
  portENTER_CRITICAL(&g_status_mux);
  const bool f = g_status.recording && g_status.first_error[0] != '\0';
  portEXIT_CRITICAL(&g_status_mux);
  return f;
}

bool start(const char *header, size_t header_len, char *name_out, size_t name_len) {
  if (!g_status.mounted || g_accept) return false;
  lock_bus();
  char name[NAME_LEN];
  if (!open_session_file(max_session_on_card() + 1, name)) {
    unlock_bus();
    return false;
  }
  g_file.write(reinterpret_cast<const uint8_t *>(header), header_len);
  g_file.flush();
  g_marks_pending.fetch_add(1);  // 印を読むまで、書き込みのタスクに前のセッションの残りを捨てさせる
  // 数え直しは、ファイルを開いたことにする前に鍵の中で（鍵を放した後だと、書き込みのタスクの確定が
  // 前のセッションの量とカード上の長さを比べて sync_lag と誤る）
  portENTER_CRITICAL(&g_status_mux);
  strncpy(g_status.name, name, NAME_LEN - 1);
  g_status.name[NAME_LEN - 1] = '\0';
  g_status.bytes = header_len;
  g_status.total_bytes = header_len;
  g_status.part = 1;
  g_status.rotate_max_ms = 0;
  g_status.rotate_pending = false;
  g_status.dropped_lines = 0;
  g_status.write_max_ms = 0;
  g_status.flush_max_ms = 0;
  g_status.buffer_high = 0;
  g_status.synced_bytes = header_len;
  g_status.sync_lag = 0;
  g_status.first_error_ms = 0;
  g_status.first_error[0] = '\0';
  g_status.first_errno = 0;
  g_status.disk_writes = 0;
  g_status.disk_fail = 0;
  g_status.disk_retry = 0;
  g_status.disk_retry_ok = 0;
  g_status.disk_write_max_ms = 0;
  g_status.disk_fail_ms = 0;
  g_status.disk_fail_card_ok = -1;
  g_status.disk_fail_sector = 0;
  g_status.disk_fail_count = 0;
  g_status.status_fail = 0;
  g_status.status_max_ms = 0;
  g_status.status_fail_ms = 0;
  g_status.read_fail = 0;
  g_status.errors_short = 0;
  g_session_events = 0;
  g_status.started_ms = millis();
  g_status.file_started_ms = g_status.started_ms;
  g_status.recording = true;
  portEXIT_CRITICAL(&g_status_mux);
  g_open = true;
  unlock_bus();
  // 開始の印を流す。受け付ける（g_accept）前に流すので、新しい行はすべて印の後ろに並ぶ
  xSemaphoreTake(g_append_lock, portMAX_DELAY);
  const bool marked = xStreamBufferSend(g_ring, &SESSION_MARK, 1, SESSION_MARK_WAIT) == 1;
  xSemaphoreGive(g_append_lock);
  if (!marked) {  // バッファが前の分で埋まったまま（書き込みのタスクが動いていない）。始めない
    lock_bus();
    g_file.close();
    g_open = false;
    unlock_bus();
    g_marks_pending.fetch_sub(1);
    portENTER_CRITICAL(&g_status_mux);
    g_status.recording = false;
    portEXIT_CRITICAL(&g_status_mux);
    set_error("ring_full");
    return false;
  }
  g_accept = true;
  if (name_out != nullptr && name_len > 0) {
    strncpy(name_out, name, name_len - 1);
    name_out[name_len - 1] = '\0';
  }
  return true;
}

bool rotate(const char *header, size_t header_len) {
  if (!g_accept || header_len > ROTATE_HEADER_MAX) return false;
  portENTER_CRITICAL(&g_status_mux);
  const bool busy = g_status.rotate_pending;
  portEXIT_CRITICAL(&g_status_mux);
  if (busy) return false;
  // 見出しを先に写す（印を置いた後は、書き込みのタスクがいつ読んでもよい）
  memcpy(g_rotate_header, header, header_len);
  g_rotate_header_len = header_len;
  xSemaphoreTake(g_append_lock, portMAX_DELAY);  // 行の途中に印が入らないように（append と同じ鍵）
  bool ok = xStreamBufferSpacesAvailable(g_ring) >= 1;
  if (ok) {
    portENTER_CRITICAL(&g_status_mux);
    g_status.rotate_pending = true;
    portEXIT_CRITICAL(&g_status_mux);
    ok = xStreamBufferSend(g_ring, &ROTATE_MARK, 1, 0) == 1;
    if (!ok) {
      portENTER_CRITICAL(&g_status_mux);
      g_status.rotate_pending = false;
      portEXIT_CRITICAL(&g_status_mux);
    }
  }
  xSemaphoreGive(g_append_lock);
  return ok;
}

void stop() {
  if (!g_accept) return;
  g_accept = false;  // これ以上は置かない
  const uint32_t t0 = millis();
  while (xStreamBufferBytesAvailable(g_ring) > 0 && millis() - t0 < STOP_DRAIN_MS) {
    csi_boot::wait_ms(10);  // loop から呼ぶので、待つ間もウォッチドッグに知らせる
  }
  lock_bus();
  if (g_open) {
    g_file.flush();
    g_file.close();
    g_open = false;
  }
  unlock_bus();
  if (xStreamBufferBytesAvailable(g_ring) > 0) set_error("stop_not_drained");
  portENTER_CRITICAL(&g_status_mux);
  g_status.recording = false;
  portEXIT_CRITICAL(&g_status_mux);
  g_status.free_bytes = measure_free();
}

bool recording() { return g_accept; }

void append(const char *data, size_t len) {
  if (!g_accept || len == 0) return;
  xSemaphoreTake(g_append_lock, portMAX_DELAY);
  if (xStreamBufferSpacesAvailable(g_ring) < len) {
    portENTER_CRITICAL(&g_status_mux);
    g_status.dropped_lines++;
    portEXIT_CRITICAL(&g_status_mux);
  } else {
    xStreamBufferSend(g_ring, data, len, 0);
    const uint32_t used = static_cast<uint32_t>(xStreamBufferBytesAvailable(g_ring));
    portENTER_CRITICAL(&g_status_mux);
    if (used > g_status.buffer_high) g_status.buffer_high = used;
    portEXIT_CRITICAL(&g_status_mux);
  }
  xSemaphoreGive(g_append_lock);
}

void inject_write_faults(uint32_t n) { g_fault_writes.store(n < FAULT_MAX ? n : FAULT_MAX); }

CardInfo card_info() {
  CardInfo info = {"none", 0};
  if (!g_status.mounted) return info;
  lock_bus();
  const sdcard_type_t t = SD.cardType();
  info.size_mb = SD.cardSize() / (1024ULL * 1024ULL);
  unlock_bus();
  switch (t) {
    case CARD_MMC: info.type = "MMC"; break;
    case CARD_SD: info.type = "SDSC"; break;
    case CARD_SDHC: info.type = "SDHC"; break;  // SDXC もこう返る（ドライバは区別しない）
    default: info.type = "unknown"; break;
  }
  return info;
}

Status status() {
  portENTER_CRITICAL(&g_status_mux);
  const Status s = g_status;
  portEXIT_CRITICAL(&g_status_mux);
  return s;
}

bool list(Print &out) {
  if (!g_status.mounted || g_accept) return false;
  lock_bus();
  File dir = SD.open(DIR);
  if (!dir) {
    unlock_bus();
    return false;
  }
  for (File f = dir.openNextFile(); f; f = dir.openNextFile()) {
    if (!f.isDirectory()) {
      out.printf("# sd file name=%s bytes=%lu\n", f.name(), static_cast<unsigned long>(f.size()));
    }
    f.close();
  }
  dir.close();
  unlock_bus();
  return true;
}

// PC の返事（"K" / "R" / "Q"）を 1 行待つ。時間切れは 0。
char wait_reply(Stream &io, void (*feed)()) {
  const uint32_t t0 = millis();
  char reply = 0;
  while (millis() - t0 < ACK_TIMEOUT_MS) {
    while (io.available() > 0) {
      const int c = io.read();
      if (c == '\n') {
        if (reply != 0) return reply;
      } else if (c == 'K' || c == 'R' || c == 'Q') {
        reply = static_cast<char>(c);
      }
    }
    if (feed != nullptr) feed();
    delay(1);
  }
  return 0;
}

int64_t send(const char *name, uint32_t offset, Stream &io, void (*feed)()) {
  if (!g_status.mounted || g_accept || session_number(name) == 0) return -1;
  char path[32];
  snprintf(path, sizeof(path), "%s/%s", DIR, name);
  lock_bus();
  File f = SD.open(path, FILE_READ);
  if (!f) {
    unlock_bus();
    return -1;
  }
  const uint32_t size = f.size();
  if (offset > size || !f.seek(offset)) {
    f.close();
    unlock_bus();
    return -1;
  }
  unlock_bus();
  while (io.available() > 0) io.read();  // 前の残り（命令の後ろの改行など）を捨てる
  io.printf("# sd get name=%s size=%lu offset=%lu chunk=%u\n", name,
            static_cast<unsigned long>(size), static_cast<unsigned long>(offset),
            static_cast<unsigned>(SEND_CHUNK));
  uint32_t pos = offset;
  const char *reason = nullptr;
  while (pos < size && reason == nullptr) {
    lock_bus();
    const int n = f.read(g_chunk, SEND_CHUNK);
    unlock_bus();
    if (n <= 0) {
      reason = "read_failed";
      break;
    }
    const uint32_t crc = crc32_le(0, g_chunk, static_cast<uint32_t>(n));
    uint32_t tries = 0;
    for (;;) {
      io.printf("#SDC %lu %d %08lx\n", static_cast<unsigned long>(pos), n,
                static_cast<unsigned long>(crc));
      io.write(g_chunk, static_cast<size_t>(n));
      io.flush();
      const char reply = wait_reply(io, feed);
      if (reply == 'K') break;
      if (reply == 'R' && ++tries <= MAX_RESEND) continue;
      reason = reply == 'Q' ? "quit" : (reply == 'R' ? "too_many_resends" : "no_reply");
      break;
    }
    if (reason == nullptr) pos += static_cast<uint32_t>(n);
  }
  lock_bus();
  f.close();
  unlock_bus();
  if (reason != nullptr) {
    io.printf("# sd get abort name=%s reason=%s at=%lu\n", name, reason,
              static_cast<unsigned long>(pos));
    return -1;
  }
  io.printf("# sd get done name=%s bytes=%lu\n", name, static_cast<unsigned long>(pos - offset));
  return static_cast<int64_t>(pos) - offset;
}

bool remove(const char *name, RemoveResult *result) {
  if (!g_status.mounted || g_accept || session_number(name) == 0) return false;
  char path[32];
  snprintf(path, sizeof(path), "%s/%s", DIR, name);
  const uint64_t free_before = measure_free();
  lock_bus();
  uint64_t bytes = 0;
  File f = SD.open(path);
  if (f) {
    bytes = f.size();
    f.close();
  }
  const bool ok = SD.remove(path);
  unlock_bus();
  if (!ok) return false;
  g_status.free_bytes = measure_free();
  if (result != nullptr) {
    result->bytes = bytes;
    result->free_before = free_before;
    result->free_after = g_status.free_bytes;
  }
  return true;
}

void set_clock(int64_t (*unix_now_sec)()) { g_clock = unix_now_sec; }

void record_event(EventKind kind, int16_t err, uint32_t dur_ms, uint32_t sector, uint32_t count) {
  queue_event(kind, err, -1, dur_ms, sector, count);
}

void print_events(Print &out, bool clear) {
  Preferences p;
  if (!p.begin(EVENT_NS, !clear)) {  // まだ一度も書いていなければ読み取りでは開けない
    out.printf("# sdevents count=0\n");
    return;
  }
  const bool valid = p.getUChar(EVENT_KEY_VER, 0) == EVENT_VERSION;
  const uint32_t seq = valid ? p.getUInt(EVENT_KEY_SEQ, 0) : 0;
  Event ring[EVENT_SLOTS] = {};
  if (valid) p.getBytes(EVENT_KEY_RING, ring, sizeof(ring));
  const uint32_t first = seq > EVENT_SLOTS ? seq - EVENT_SLOTS + 1 : 1;
  out.printf("# sdevents count=%lu total=%lu\n", (unsigned long)(seq >= first ? seq - first + 1 : 0),
             (unsigned long)seq);
  for (uint32_t i = first; i <= seq; i++) {
    const Event &ev = ring[(i - 1) % EVENT_SLOTS];
    out.printf("# sdevent seq=%lu kind=%s unix=%ld up_s=%lu session=S%04u session_s=%ld "
               "dur_ms=%u card_ok=%d err=%d sector=%lu count=%u\n",
               (unsigned long)ev.seq, event_kind_name(ev.kind), (long)ev.unix_s,
               (unsigned long)ev.uptime_s, (unsigned)ev.session, (long)ev.session_s,
               (unsigned)ev.dur_ms, (int)ev.card_ok, (int)ev.err, (unsigned long)ev.sector,
               (unsigned)ev.count);
  }
  if (clear) {
    p.clear();
    out.printf("# sdevents cleared\n");
  }
  p.end();
}

}  // namespace sd_log
#endif
