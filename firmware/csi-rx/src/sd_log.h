// 受信機の出力を SD カードに保存する（M5Stack Basic の受信機用。設計は docs/decisions/0006）。
//
// - 既定は書き込みなし。セッション（開始〜停止）ごとに /CSI/S0001.csv の形のファイルに書く
// - 長いセッションは rotate で次の通し番号のファイルに切り替える（2026-10-09。main が 1 時間ごとに呼ぶ）。
//   FAT32 は 1 ファイル 4 GiB まで（約 35 時間で届く）で、大きいファイルは PC で扱いにくく、壊れたときの
//   被害も大きいため。切り替えの間も行はリングバッファにたまるので落とさない。各ファイルの先頭に見出しを
//   書き直すので 1 本だけでも読める
// - 中身はシリアルの出力と同じ形式（CSI 行と # 行）。PC の解析ツールでそのまま読める
// - 取りこぼし対策: 書き込みの要求は RING_BYTES のリングバッファに置くだけで待たない。SD への書き込みは
//   専用のタスクが行う。SD は内部の整理でときどき止まる（規格上の書き込みの待ちの上限は SDHC で 250 ms）。
//   40 KB/秒なら約 1.2 秒分ためられる。あふれたら行ごと捨てて数える（黙って落とさない）
// - SD カードと液晶は同じ SPI バスを使う（M5Stack Basic: SCK 18・MOSI 23・MISO 19、SD の CS 4・液晶の CS 14）。
//   バスの鍵（bus_lock）で、どちらか一方だけが使う
// - 1 秒ごとに確定（flush）する。電源を切る・カードを抜く前はセッションを止める
// - 確定の後にカード上のファイルの長さを読み直し、書いたつもりの量と比べる（2026-10-08）。
//   flush（fflush・fsync）は失敗を返さないので、カードに届かなくなったことはこれでしか分からない
//   （2026-10-07 の夜、エラー 0・REC 表示のまま 17:59 以降がカードに残らなかった）
// - SD のドライバ（Arduino の sd_diskio）の書き込み・状態の問い合わせ・読み出しを包み、時間と失敗を測る（2026-10-08）。
//   失敗したらその場でカードに状態を問い合わせ（SEND_STATUS）、応答したかを残す。
//   約 500 ms で失敗 = カードが忙しいまま時間切れ（ドライバの待ちの上限）、すぐ失敗 + 応答なし = カードが
//   初期化前の状態に戻った（電源の瞬断などでカードがリセットされた疑い）と読む
// - 失敗・再起動からの再開を「出来事」として NVS に残す（直近 EVENT_SLOTS 件。カードが書けなくても残る）。
//   PC から SDEVENTS で読む。セッションごとに残す数に上限（失敗が続いても NVS を書き続けない）
// - CSI_SD=1 のビルド（platformio.ini の m5stack-r2）だけで有効
#pragma once

#include <Arduino.h>

#ifndef CSI_SD
#define CSI_SD 0
#endif

#if CSI_SD
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>

namespace sd_log {

constexpr size_t NAME_LEN = 16;       // "S0001.csv" と終端
constexpr size_t ERROR_LEN = 24;

struct Status {
  bool mounted;                // カードを読めた
  bool disk_hooked;            // ドライバの書き込みを包めた（時間・失敗を測れる）
  bool recording;              // セッション中
  char name[NAME_LEN];         // 今のファイル名（セッションが終わった後は最後のもの）
  uint64_t bytes;              // 今のファイルに書いた量 [byte]
  uint64_t total_bytes;        // このセッション（切り替えた全ファイル）に書いた量 [byte]
  uint32_t part;               // 今のファイルがセッションの何本目か（1〜）
  uint32_t file_started_ms;    // 今のファイルを開いた時刻 [ms]（millis）
  uint32_t rotate_max_ms;      // 切り替え（閉じる・開く・見出し）1 回の最大 [ms]（このセッション）
  bool rotate_pending;         // 切り替えを頼んで、まだ済んでいない
  uint32_t dropped_lines;      // バッファがあふれて捨てた行（このセッション）
  uint32_t write_max_ms;       // 1 回の書き込みの最大 [ms]（このセッション）
  uint32_t flush_max_ms;       // 確定の最大 [ms]（このセッション）
  uint32_t buffer_high;        // バッファの最大の使用量 [byte]（このセッション）
  uint32_t errors;             // 書き込みの失敗の数（起動から）
  uint64_t free_bytes;         // 空き容量 [byte]（取り付け時・停止時に測る）
  uint32_t started_ms;         // セッションを始めた時刻 [ms]（millis）
  char last_error[ERROR_LEN];  // 最後の失敗の理由
  int last_errno;              // 最後の失敗の errno（0 = なし）
  uint32_t first_error_ms;     // このセッションで最初に失敗した時刻 [ms]（millis。0 = なし）
  char first_error[ERROR_LEN]; // このセッションで最初の失敗の理由（後の失敗で上書きしない）
  int first_errno;             // その errno
  uint64_t synced_bytes;       // 最後の確定の後にカード上で読んだファイルの長さ [byte]（stat）
  uint32_t sync_lag;           // 確定の後でカード上の長さが bytes と合わなかった回数（このセッション）
  // ドライバの層（このセッション）
  uint32_t disk_writes;        // ドライバの書き込みの回数（FatFs からの呼び出しの数。再試行は数えない）
  uint32_t disk_fail;          // そのうち再試行しても失敗した回数（FatFs に失敗を返した＝ファイルが打ち切られる）
  uint32_t disk_retry;         // 書き直した回数（試みの数。1 回の呼び出しで最大 DISK_RETRY_MAX 回）
  uint32_t disk_retry_ok;      // 書き直しで救えた呼び出しの数
  uint32_t disk_write_max_ms;  // ドライバの書き込み 1 回の最大 [ms]
  uint32_t disk_fail_ms;       // 最後に失敗した書き込みにかかった時間 [ms]
  int8_t disk_fail_card_ok;    // 最後の失敗の直後にカードが状態の問い合わせに応答したか（1/0。-1 = 失敗なし）
  uint32_t disk_fail_sector;   // 最後に失敗した書き込みの先頭セクタ
  uint32_t disk_fail_count;    // そのセクタ数
  // FatFs は書き込みの前に毎回カードの状態を問い合わせ、答えなければ書かずに断る（errno=9 EBADF）
  // （2026-10-08 にカードを抜く試験で確認。disk_fail は 0 のままだった）
  uint32_t status_fail;        // 状態の問い合わせの失敗の回数
  uint32_t status_max_ms;      // 状態の問い合わせ 1 回の最大 [ms]
  uint32_t status_fail_ms;     // 最後に失敗した問い合わせにかかった時間 [ms]
  uint32_t read_fail;          // ドライバの読み出しの失敗の回数
  uint32_t errors_short;       // write() が頼んだ量より少なく返った回数
};

// 出来事の種類（NVS に残す。番号は変えない）
enum class EventKind : uint8_t {
  DISK_FAIL = 1,    // ドライバの書き込みの失敗（dur_ms・card_ok・sector・count）
  WRITE_SHORT = 2,  // write() が頼んだ量より少なく返った（err = errno）
  SYNC_LAG = 3,     // 確定の後にカード上の長さが合わない（err = errno）
  RESUME = 4,       // セッション中に再起動して再開した（err = esp_reset_reason の値）
  STATUS_FAIL = 5,  // カードが状態の問い合わせに答えなかった（dur_ms。約 500 ms ならカードが忙しいまま）
  READ_FAIL = 6,    // ドライバの読み出しの失敗（dur_ms・sector・count）
  RECOVER = 7,      // 立て直した（err = 段: 1 = 開き直し・2 = 付け直し、sector = 書けなかった時間 [ms]、count = 試した回数）
  DISK_RETRY = 8,   // 書き直しで救えた（err = 書き直した回数、dur_ms = 書き直しを含む時間、card_ok・sector・count）。
                    // 書き直すのは RES_ERROR でカードが応答したときだけ。セッションで最初の 1 回だけ残す
                    // （回数は # sdstat の disk_retry・disk_retry_ok）
};

// 出来事 1 件（NVS に配列で置く。並びを変えたら EVENT_VERSION を上げる）
struct Event {
  uint32_t seq;       // 通し番号（1〜。消すまで増える）
  int32_t unix_s;     // 時刻 [sec]（unix。分からなければ 0）
  uint32_t uptime_s;  // 起動からの時間 [sec]
  int32_t session_s;  // セッションの開始からの時間 [sec]（-1 = セッション外）
  uint16_t session;   // セッションの番号（S0003 なら 3。0 = なし）
  uint8_t kind;       // EventKind
  int8_t card_ok;     // DISK_FAIL の直後にカードが応答したか（1/0。-1 = 該当なし）
  uint16_t dur_ms;    // DISK_FAIL にかかった時間 [ms]（上限 65535）
  int16_t err;        // errno・再起動の理由など（kind による）
  uint32_t sector;    // DISK_FAIL の先頭セクタ
  uint16_t count;     // DISK_FAIL のセクタ数
  uint16_t reserved;
};

constexpr size_t EVENT_SLOTS = 16;  // NVS に残す件数（古いものから上書き）

// 時刻を返す関数（unix [sec]。分からなければ 0）を渡す。出来事に時刻を付ける
void set_clock(int64_t (*unix_now_sec)());

// 出来事を待ち行列に置く（待たない。NVS には書き込みのタスクが書く）。各欄の意味は kind による
void record_event(EventKind kind, int16_t err, uint32_t dur_ms = 0, uint32_t sector = 0,
                  uint32_t count = 0);

// NVS の出来事を古い順に "# sdevent ..." の形で out に書く。clear なら書いた後に消す
void print_events(Print &out, bool clear);

// 書き込みのタスクを起こし、カードを取り付ける。bus_lock は液晶と共有する SPI バスの鍵。
// カードが無くても false を返すだけで、後から remount で付けられる。
bool begin(SemaphoreHandle_t bus_lock);

// 新しいセッションを始める（番号は /CSI の中の最大 + 1）。header をファイルの先頭に書く。
// 成功ならファイル名を name_out に入れて true。
bool start(const char *header, size_t header_len, char *name_out, size_t name_len);

// 次の通し番号のファイルに切り替えるよう頼む（待たない）。今までに置いた行を書き終えたところで今のファイルを
// 閉じ、次のファイルを開いて header を書く。その後に置いた行は次のファイルに入る。
// セッション外・切り替えの途中なら false。次のファイルを開けなければ失敗（failed）になり、立て直しに入る。
bool rotate(const char *header, size_t header_len);

// セッションを止める（バッファに残った分を書き終えて閉じる）。
void stop();

// 書き込みに失敗しているセッションを、待たずに打ち切る（バッファの残りは捨てる。書けないので待つ意味がない）。
void abort();

// カードを付け直す（SD.end → SD.begin。ドライバも包み直す）。セッション外で呼ぶ。カードが無ければ false。
bool remount();

// セッション中で、書き込みに失敗している（first_error がある）。立て直しの合図
bool failed();

bool recording();

// 1 行（改行つき）を書き込みの待ち行列に置く（待たない）。セッション外・あふれは何もしない（あふれは数える）。
void append(const char *data, size_t len);

Status status();

// カードの種類（"SDHC" など。付いていなければ "none"）と容量 [MB]。セッションの見出し（# env）用
struct CardInfo {
  const char *type;
  uint64_t size_mb;
};
CardInfo card_info();

// 試験用: 次の n 回のドライバの書き込みを、カードに触らずに失敗（RES_ERROR）にする。書き直しも 1 回と数える
constexpr uint32_t FAULT_MAX = 10;
void inject_write_faults(uint32_t n);

// ---- PC への転送（セッション外だけ。呼んだタスクで処理する） ----
// /CSI のファイルを "# sd file name=S0001.csv bytes=123" の形で out に書く。
bool list(Print &out);
// ファイルを offset から送る（1 塊ずつ確認しながら）。"#SDC <offset> <len> <crc32>" の行と len バイトを送り、
// PC の返事を待つ: "K" = 次へ、"R" = 同じ塊をもう一度、"Q" = やめる。ACK_TIMEOUT_MS 返事がなければやめる。
// PC が受け取れる速さに合わせるので、シリアルの上限の速さで流し続けて取りこぼすことがない
// （2026-10-07: 流し続ける方式では 4 KB の塊がときどき壊れ、送り直しも前の残りと混ざった）。
// feed は待つ間も呼ぶ（ウォッチドッグに知らせる）。送ったバイト数を返す（失敗・中断は -1）。
int64_t send(const char *name, uint32_t offset, Stream &io, void (*feed)());

// 消したファイルの大きさと、消す前後の空き（FatFs の数え。2026-10-10: 24 本を消しても空きが増えず、
// 再起動しても戻らなかった。その場で気づけるように返す）
struct RemoveResult {
  uint64_t bytes;        // 消したファイルの大きさ [byte]
  uint64_t free_before;  // 消す前の空き [byte]
  uint64_t free_after;   // 消した後の空き [byte]
};
bool remove(const char *name, RemoveResult *result = nullptr);

}  // namespace sd_log
#endif
