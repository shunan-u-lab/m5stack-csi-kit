// CSI 送信機: ESP-NOW のブロードキャストを固定間隔・固定チャンネル・固定レートで送り続ける。
//
// ESP32 は「自分が受信したパケット」からしか CSI を得られない。ルーターに繋いで
// 待つだけでは 5 秒間 0 件のこともあった（2026-09-23 実測）。受信側のサンプリング
// レートは送り込むパケットの量で決まるため、専用の送信機を置く。
//
// - ルーターを経由しない（ESP-NOW）ので、測れるのは送信機→受信機の経路そのもの
// - レートを固定する。レート適応が働くと、振幅が環境ではなく変調方式の変化で動く
// - 連番を載せ、受信側で取りこぼしと間隔の乱れを検出できるようにする
// - 内蔵 LED（GPIO2）で動作を示す: 1 秒に 1 回の点滅 = 送信中（受信機は 2 回）。切り替え = 素早く 3 回、
//   番犬・送信エラー = 1 秒の速い点滅、初期化失敗 = 速い点滅が続く
// - シリアルの命令でチャンネルを切り替えられる（チャンネルの掃引など）。起動時は WIFI_CHANNEL。
//     CH <1-13>  チャンネルを切り替え、読み戻して確かめる → [tx] ack cmd=CH channel=<n>
//     WHO        役割・MAC・今のチャンネルを返す（再起動せずにポートを見分ける）
//   失敗は [tx] err cmd=<命令> reason=<理由> を返し、送信は止めない
// - 受信機からの無線の命令でもチャンネルを切り替える（S を PC から離して電源だけで使うため）。
//   受信機（g_rx_mac。PAIRSET で NVS に置く）から ESP-NOW で届く制御パケット（CsiCtrl）だけを受け付ける:
//     SET_CH    了解（ACK。範囲外なら NACK）をブロードキャストで返してから切り替える
//     KEEPALIVE 受信機が 1 秒ごとに送る合図
//   番犬: 起動時のチャンネル以外にいるとき、受信機の合図が WATCHDOG_MS 途絶えたら起動時のチャンネルに戻る
//   （受信機を見失っても、両方が起動時のチャンネルに集まって通信が戻る）
// - 制御パケットは共有の鍵で署名を確かめる（firmware/shared/CsiAuth・2026-10-07）。送り元の MAC だけでは
//   誰でも偽れたため。データのパケットに合言葉（ctrl_nonce）を載せ、受信機はそれを含めて署名する。
//   SET_CH を受け入れたら合言葉を変えるので、録音した命令の流し直しは通らない。鍵なしでビルドすると
//   無線の命令を一切受け付けない（シリアルの CH は使える）。署名の合わない制御パケットは ctrl_bad に数える
// - 送信の出力: 起動時は CSI_TX_POWER_QDBM（0.25 dBm 単位。0 = ESP-IDF の既定のまま）。
//     TXP <2-20>  出力の上限 [dBm] を変えて読み戻す → [tx] ack cmd=TXP dbm=<実際の値>
//   近い距離では出力を下げても受信に余裕がある。下げるほど壁の外まで届きにくくなる。
//   今の出力はデータのパケット（版 3 の tx_qdbm）に載せ、受信機の液晶と記録（# stats）に出す
// - チップの温度（内蔵センサーの目安・℃）もデータのパケット（版 4 の tx_temp_c）に載せ、受信機の記録
//   （# stats の peer_temp_c）に残す（2026-10-10。晩ごとの条件の違いを比べるため）
// - 受信機からの無線の命令でも出力を変える（SET_TXP・2026-10-07）。変えた出力で了解（TXP_ACK・実際の値）を
//   返し、合言葉を変えて「試し」に入る。受信機は新しい出力の電波（新しい合言葉）が届くのを確かめて確認
//   （TXP_CONFIRM・新しい合言葉で署名）を返す。TXP_CONFIRM_MS 以内に確認が来なければ元の出力に戻す
//   （下げすぎて受信機に届かなくなっても自分で戻る）。範囲外は NACK
// - 見張りと再起動（2026-10-07）: loop が 5 秒止まると自動で再起動する（ウォッチドッグ。firmware/shared/CsiBoot）。
//   起動時の表示に前回の再起動の理由（boot=poweron / sw / task_wdt / panic …）。受信機の署名付きの
//   REBOOT で再起動する（了解を返してから。出力などを既定に戻す最終手段）。
//     WDTTEST  わざと loop を止めてウォッチドッグを確かめる（USB からだけ。5 秒ほどで再起動する）
//     REBOOT   再起動する（USB から。次の起動の表示は boot=sw）

#include <Arduino.h>
#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <esp_system.h>
#include <stddef.h>

#include <CsiAuth.h>
#include <CsiBoot.h>
#include <CsiPair.h>

// M5Stack の液晶に稼働状況を出すか（1 = 出す。platformio.ini の M5Stack 用 env だけで 1 にする）
#ifndef CSI_M5_LCD
#define CSI_M5_LCD 0
#endif
#if CSI_M5_LCD
#include <CsiButtons.h>
#include <CsiLcd.h>
// ボタン: C の長押しで画面の明るさ（普通 → 暗い → 消灯。覚える）。A・B・C の短押しは何もしない
static csi_buttons::Buttons g_buttons;
#endif
// 液晶に出す機器番号と相手の番号（M5Stack 用 env の build_flags で与える）
#ifndef CSI_LABEL
#define CSI_LABEL "T1"
#endif
#ifndef CSI_PEER_LABEL
#define CSI_PEER_LABEL "R1"
#endif

// ファームウェアの版（platformio.ini が firmware/shared/fw_version.py で与える。例 "1007-db82545"）
#ifndef CSI_FW_VERSION
#define CSI_FW_VERSION "unknown"
#endif
// 起動時の送信出力の上限 [0.25 dBm]（esp_wifi_set_max_tx_power の単位）。0 = ESP-IDF の既定のまま
#ifndef CSI_TX_POWER_QDBM
#define CSI_TX_POWER_QDBM 0
#endif

// ---- 設定（受信側と必ず揃える） ------------------------------------------

// 起動時の送信チャンネル（受信機の WIFI_CHANNEL と同じ）。CH 命令で変えられる（電源を切ると戻る）
constexpr uint8_t WIFI_CHANNEL = 11;
// CH 命令で受け付ける範囲。日本では ch1〜13 が OFDM で使える（ch14 は DSSS のみで 11n は不可）
constexpr uint8_t CHANNEL_MIN = 1;
constexpr uint8_t CHANNEL_MAX = 13;
// 国の設定。既定（"CN"）のままにせず明示する。802.11d は使わない（AP に接続しないため）
constexpr const char *COUNTRY_CODE = "JP";
// 命令 1 行の最大長 [byte]（改行を除く）。超えたら捨てて err を返す
constexpr size_t COMMAND_MAX_LEN = 64;  // PAIRSET（MAC 12 文字・鍵 32 文字）が入る長さ
// 命令を受け付ける受信機の STA MAC。起動時に NVS から読む（PAIRSET で置く。firmware/shared/CsiPair）。
// 未設定なら全部 0 のままで、どの受信機の命令も受け付けない（データの送信は続ける）
static uint8_t g_rx_mac[ESP_NOW_ETH_ALEN] = {};
static bool g_paired = false;
// 番犬: 起動時のチャンネル以外で、受信機の合図がこれだけ途絶えたら戻る [ms]（受信機の合図は 1 秒ごと）
constexpr uint32_t WATCHDOG_MS = 4000;
// 了解を送る回数と間隔 [ms]（ブロードキャストは再送されないので、取りこぼしに備えて繰り返す。
// 1 回だけだと 13 回中 1 回届かなかった: 2026-09-23 実測）。送り終えてから切り替える
constexpr uint32_t ACK_REPEAT = 3;
constexpr uint32_t ACK_INTERVAL_MS = 5;
// 送信間隔 [ms]。10 ms = 100 Hz
constexpr uint32_t SEND_INTERVAL_MS = 10;
// 物理層レート。802.11n MCS0・通常 GI。受信側で HT-LTF まで CSI が取れる
constexpr wifi_phy_rate_t PHY_RATE = WIFI_PHY_RATE_MCS0_LGI;
// 状況報告の間隔 [ms]
constexpr uint32_t REPORT_INTERVAL_MS = 1000;
// 初期化失敗時に理由を出し直す間隔 [ms]
constexpr uint32_t HALT_REPEAT_MS = 2000;
// シリアル速度 [bit/s]
constexpr uint32_t SERIAL_BAUD = 115200;
// 液晶: 送信レートがこれを下回ったら注意の色にする [Hz]（設定は 100 Hz）
constexpr float LCD_RATE_WARN_HZ = 90.0f;
// 液晶: 受信機の合図（1 秒ごと）がこれだけ途絶えたら「相手と通じていない」とみなす [ms]
constexpr uint32_t LCD_PEER_LOST_MS = 3000;

// ペイロードの識別子（"CSIT" のリトルエンディアン）。受信側で自分の送信機のパケットを見分ける
constexpr uint32_t PAYLOAD_MAGIC = 0x54495343;
// ペイロードの版。形式を変えたら上げる（2: 末尾に ctrl_nonce を足した。3: さらに tx_qdbm。
// 4: さらに tx_temp_c。先頭 16 バイトは版 1 と同じ）
constexpr uint16_t PAYLOAD_VERSION = 4;
// チップの温度を読み直す間隔 [ms]（内蔵センサーの読み取りを送信のたびにしない）
constexpr uint32_t TEMP_INTERVAL_MS = 1000;

constexpr uint8_t BROADCAST_ADDR[ESP_NOW_ETH_ALEN] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};

// 制御パケットの識別子（"CSIC" のリトルエンディアン）と版。受信機（firmware/csi-rx）と同じ
constexpr uint32_t CTRL_MAGIC = 0x43495343;
constexpr uint16_t CTRL_VERSION = 2;  // 2: 合言葉と署名を足した（2026-10-07）
enum CtrlCmd : uint8_t {
  CTRL_SET_CH = 1,     // 受信機 → 送信機: チャンネルを変えよ
  CTRL_ACK = 2,        // 送信機 → 受信機: 了解（これから切り替える）
  CTRL_KEEPALIVE = 3,  // 受信機 → 送信機: 合図（番犬を止める）
  CTRL_NACK = 4,       // 送信機 → 受信機: 範囲外なので切り替えない（SET_TXP の範囲外にも使う）
  CTRL_SET_TXP = 5,    // 受信機 → 送信機: 出力をこの値にせよ（channel の欄に 0.25 dBm 単位）
  CTRL_TXP_ACK = 6,    // 送信機 → 受信機: 変えた（channel の欄に実際の値・0.25 dBm 単位）
  CTRL_TXP_CONFIRM = 7,  // 受信機 → 送信機: 新しい出力の電波が届いた（新しい合言葉で署名）
  CTRL_REBOOT = 8,     // 受信機 → 送信機: 再起動せよ（了解 ACK を返してから再起動する）
};
// 再起動の了解を送ってから再起動するまでの待ち [ms]（了解とシリアルの表示を出し切る）
constexpr uint32_t REBOOT_DELAY_MS = 50;
// WDTTEST で loop を止める時間 [ms]（ウォッチドッグの 5 秒より長く）
constexpr uint32_t WDT_TEST_BLOCK_MS = 10000;
// 出力の試し: この間に確認が来なければ元の出力に戻す [ms]
constexpr uint32_t TXP_CONFIRM_MS = 3000;

// ---- ペイロード -----------------------------------------------------------

struct __attribute__((packed)) CsiTxPayload {
  uint32_t magic;         // PAYLOAD_MAGIC
  uint16_t version;       // PAYLOAD_VERSION
  uint16_t interval_ms;   // 送信間隔 [ms]
  uint32_t seq;           // 連番（0 始まり・送信のたびに +1）
  uint32_t tx_ms;         // 送信時刻 [ms]（起動からの millis()）
  uint32_t ctrl_nonce;    // 制御パケットの合言葉（起動ごとの乱数。SET_CH を受け入れるたびに変える）
  int8_t tx_qdbm;         // 今の送信出力の上限 [0.25 dBm]（版 3 から）
  int8_t tx_temp_c;       // チップの温度 [℃]（内蔵センサーの目安。室温ではない。版 4 から）
};

// 制御パケット（リトルエンディアン・24 バイト）。署名は先頭 CTRL_SIGNED_LEN バイトに対して
struct __attribute__((packed)) CsiCtrl {
  uint32_t magic;    // CTRL_MAGIC
  uint16_t version;  // CTRL_VERSION
  uint8_t cmd;       // CtrlCmd
  uint8_t channel;   // SET_CH / ACK / NACK の対象チャンネル
  uint32_t token;    // 受信機が命令ごとに変える番号（了解と命令の対応づけ）
  uint32_t nonce;    // 送信機の合言葉（受信機はデータのパケットから読む）
  uint8_t tag[csi_auth::TAG_LEN];  // 署名
};
constexpr size_t CTRL_SIGNED_LEN = offsetof(CsiCtrl, tag);

// 送信出力の上限として受け付ける範囲 [dBm]（ESP32 の esp_wifi_set_max_tx_power は 2〜20 dBm）
constexpr float TX_POWER_MIN_DBM = 2.0f;
constexpr float TX_POWER_MAX_DBM = 20.0f;
constexpr float QDBM_PER_DBM = 4.0f;  // esp_wifi の出力の単位は 0.25 dBm

// ---- 計数（送信完了コールバックは Wi-Fi タスクで走る） ------------------------

static volatile uint32_t g_cb_ok = 0;
static volatile uint32_t g_cb_fail = 0;

// 今の送信チャンネル（loop だけが書く）
static uint8_t g_channel = WIFI_CHANNEL;

// 受信機からの命令（受信コールバックが書き、loop が読む）
static volatile bool g_ctrl_pending = false;
static volatile uint8_t g_ctrl_channel = 0;
static volatile uint32_t g_ctrl_token = 0;
static volatile uint32_t g_last_heard_ms = 0;  // 受信機の制御パケットを最後に受けた時刻 [ms]
static volatile uint32_t g_ctrl_rx = 0;        // 受けた制御パケットの数（署名の合ったもの）
static volatile uint32_t g_ctrl_bad = 0;       // 署名・合言葉の合わない制御パケットの数

// 認証（setup で鍵を読む）。g_auth が false なら無線の命令を受け付けない
static uint8_t g_key[csi_auth::KEY_LEN] = {};
static bool g_auth = false;
static volatile uint32_t g_nonce = 0;  // 今の合言葉（データのパケットに載せる）

// 送信出力（loop だけが書く。データのパケットと液晶に出す）
static int8_t g_tx_qdbm = 0;  // 今の出力の上限 [0.25 dBm]（読み戻した値）
// 受信機からの出力の命令（受信コールバックが書き、loop が読む）
static volatile bool g_txp_pending = false;
static volatile uint8_t g_txp_qdbm = 0;
static volatile uint32_t g_txp_token = 0;
static volatile bool g_txp_confirmed = false;
// 出力の試し（loop だけが書く。確認は受信コールバックが g_txp_confirmed を立てる）
static bool g_txp_trial = false;
static volatile uint32_t g_txp_trial_token = 0;  // 受信コールバックも読む
static uint32_t g_txp_deadline_ms = 0;
static int8_t g_txp_prev_qdbm = 0;
// 受信機からの再起動の命令（受信コールバックが書き、loop が読む）
static volatile bool g_reboot_pending = false;
static volatile uint32_t g_reboot_token = 0;

// 合言葉を変える（前と同じ値・0 を避ける）。
static void rotate_nonce() {
  uint32_t next = 0;
  do {
    next = esp_random();
  } while (next == 0 || next == g_nonce);
  g_nonce = next;
}

// 受信コールバック（Wi-Fi タスク。重い処理をしない）: 受信機の制御パケットだけを控える。
// 署名の確認（HMAC-SHA256・20 バイト程度）は数十 µs で、合図は 1 秒に 1 回なので重くない
static void on_recv(const uint8_t *mac, const uint8_t *data, int len) {
  if (mac == nullptr || memcmp(mac, g_rx_mac, ESP_NOW_ETH_ALEN) != 0) return;
  if (data == nullptr || len != static_cast<int>(sizeof(CsiCtrl))) return;
  CsiCtrl ctrl;
  memcpy(&ctrl, data, sizeof(ctrl));
  if (ctrl.magic != CTRL_MAGIC) return;
  if (ctrl.version != CTRL_VERSION || !g_auth || ctrl.nonce != g_nonce ||
      !csi_auth::verify(g_key, reinterpret_cast<const uint8_t *>(&ctrl), CTRL_SIGNED_LEN,
                        ctrl.tag)) {
    g_ctrl_bad++;
    return;
  }
  g_last_heard_ms = millis();
  g_ctrl_rx++;
  if (ctrl.cmd == CTRL_SET_CH && !g_ctrl_pending) {
    g_ctrl_channel = ctrl.channel;
    g_ctrl_token = ctrl.token;
    g_ctrl_pending = true;
  } else if (ctrl.cmd == CTRL_SET_TXP && !g_txp_pending) {
    g_txp_qdbm = ctrl.channel;
    g_txp_token = ctrl.token;
    g_txp_pending = true;
  } else if (ctrl.cmd == CTRL_TXP_CONFIRM && ctrl.token == g_txp_trial_token) {
    g_txp_confirmed = true;
  } else if (ctrl.cmd == CTRL_REBOOT && !g_reboot_pending) {
    g_reboot_token = ctrl.token;
    g_reboot_pending = true;
  }
}

static void on_sent(const uint8_t *mac, esp_now_send_status_t status) {
  (void)mac;
  if (status == ESP_NOW_SEND_SUCCESS) {
    g_cb_ok++;
  } else {
    g_cb_fail++;
  }
}

// ---- 内蔵 LED（動作の表示） -------------------------------------------------
// 光り方は「点灯 [ms], 消灯 [ms], 点灯, …」の並び（点灯から始まる）。優先度の高いものが割り込み、
// 低いものは今の光り方が終わるまで待たずに捨てる。loop から led_update() を呼んで進める。
// 送受信は 100 Hz で目に追えないので、連番が LED_SYNC_EVERY の倍数のパケット（1 秒に 1 回）を合図に
// 光る。送信機は送った瞬間、受信機は同じパケットを受けた瞬間に光るので、2 台がほぼ同時に光る。

// 内蔵 LED のピン。M5Stack Basic には無い（GPIO2 に外付けすれば光る。付けなくても害はない）
constexpr uint8_t LED_PIN = 2;
constexpr uint32_t LED_SYNC_EVERY = 100;  // この連番ごとに光る（100 Hz なら 1 秒ごと）
enum LedPriority : uint8_t { LED_IDLE = 0, LED_NORMAL = 1, LED_EVENT = 2, LED_ERROR = 3 };
constexpr uint16_t LED_BEAT_TX[] = {80};                      // 送信機: 1 回
constexpr uint16_t LED_BEAT_RX[] = {80, 120, 80};             // 受信機: 2 回（受けた瞬間と 0.2 秒後）
constexpr uint16_t LED_WAITING[] = {20};                      // 受信機が受けていない: 短くちらつく
constexpr uint16_t LED_SWITCH[] = {40, 60, 40, 60, 40};       // チャンネルの切り替え: 素早く 3 回
constexpr uint16_t LED_ALERT[] = {50, 50, 50, 50, 50, 50, 50, 50, 50, 50,
                                  50, 50, 50, 50, 50, 50, 50, 50, 50};  // 1 秒の速い点滅
constexpr uint32_t LED_FATAL_HALF_MS = 50;                    // 初期化失敗: 速い点滅を続ける

static const uint16_t *g_led_steps = nullptr;
static size_t g_led_n = 0;
static size_t g_led_i = 0;
static uint32_t g_led_since_ms = 0;
static uint8_t g_led_prio = LED_IDLE;

static void led_init() {
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);
}

template <size_t N>
static void led_start(const uint16_t (&steps)[N], LedPriority prio) {
  if (g_led_steps != nullptr && prio < g_led_prio) return;  // 優先度の低いものは捨てる
  g_led_steps = steps;
  g_led_n = N;
  g_led_i = 0;
  g_led_prio = prio;
  g_led_since_ms = millis();
  digitalWrite(LED_PIN, HIGH);
}

static void led_update() {
  if (g_led_steps == nullptr) return;
  const uint32_t now = millis();
  if (now - g_led_since_ms < g_led_steps[g_led_i]) return;
  g_led_since_ms = now;
  if (++g_led_i >= g_led_n) {
    digitalWrite(LED_PIN, LOW);
    g_led_steps = nullptr;
    g_led_prio = LED_IDLE;
    return;
  }
  digitalWrite(LED_PIN, g_led_i % 2 == 0 ? HIGH : LOW);
}

// 初期化失敗（halt）中の点滅。ms の間、速く点滅し続ける（Wi-Fi が動いていなくても使える）。
static void led_fatal_blink(uint32_t ms) {
  const uint32_t t0 = millis();
  bool on = false;
  while (millis() - t0 < ms) {
    on = !on;
    digitalWrite(LED_PIN, on ? HIGH : LOW);
    delay(LED_FATAL_HALF_MS);
  }
}

// ---- 失敗時の挙動: 黙って進まず、理由を出し続けて止まる ------------------------

[[noreturn]] static void halt(const char *what, esp_err_t err) {
  for (;;) {
    Serial.printf("[tx] FATAL: %s failed: %s (0x%x)\n", what, esp_err_to_name(err), err);
    led_fatal_blink(HALT_REPEAT_MS);
  }
}

static void check(esp_err_t err, const char *what) {
  if (err != ESP_OK) {
    halt(what, err);
  }
}

// ---- 初期化 ---------------------------------------------------------------

static void setup_radio() {
  WiFi.mode(WIFI_STA);   // Wi-Fi を起動する（どこにも接続しない）
  WiFi.disconnect();

  check(esp_wifi_set_ps(WIFI_PS_NONE), "esp_wifi_set_ps");  // 省電力で送信が間引かれるのを防ぐ
  check(esp_wifi_set_protocol(WIFI_IF_STA,
                              WIFI_PROTOCOL_11B | WIFI_PROTOCOL_11G | WIFI_PROTOCOL_11N),
        "esp_wifi_set_protocol");
  check(esp_wifi_set_bandwidth(WIFI_IF_STA, WIFI_BW_HT20), "esp_wifi_set_bandwidth");
  check(esp_wifi_set_country_code(COUNTRY_CODE, false), "esp_wifi_set_country_code");
  check(esp_wifi_set_channel(WIFI_CHANNEL, WIFI_SECOND_CHAN_NONE), "esp_wifi_set_channel");

  check(esp_now_init(), "esp_now_init");
  check(esp_wifi_config_espnow_rate(WIFI_IF_STA, PHY_RATE), "esp_wifi_config_espnow_rate");
  check(esp_now_register_send_cb(on_sent), "esp_now_register_send_cb");
  check(esp_now_register_recv_cb(on_recv), "esp_now_register_recv_cb");

  esp_now_peer_info_t peer = {};
  memcpy(peer.peer_addr, BROADCAST_ADDR, ESP_NOW_ETH_ALEN);
  peer.channel = 0;  // 0 = 今のチャンネルで送る（CH 命令で切り替えても宛先を直さずに済む）
  peer.ifidx = WIFI_IF_STA;
  peer.encrypt = false;  // ブロードキャストは暗号化できない。制御パケットは署名で守る
  check(esp_now_add_peer(&peer), "esp_now_add_peer");

  if (CSI_TX_POWER_QDBM > 0) {
    check(esp_wifi_set_max_tx_power(static_cast<int8_t>(CSI_TX_POWER_QDBM)),
          "esp_wifi_set_max_tx_power");
  }
}

// 組（受信機の MAC と鍵）を NVS から読み、合言葉を決める。未設定なら無線の命令を受け付けない。
static void setup_auth() {
  const csi_pair::Pair pair = csi_pair::load();
  g_paired = pair.ok;
  if (pair.ok) {
    memcpy(g_rx_mac, pair.peer, sizeof(g_rx_mac));
    memcpy(g_key, pair.key, sizeof(g_key));
  }
  g_auth = pair.ok;
  rotate_nonce();
}

// 今の送信出力の上限 [dBm]（読めなければ負の値）。データのパケット用の g_tx_qdbm も更新する。
static float tx_power_dbm() {
  int8_t q = 0;
  if (esp_wifi_get_max_tx_power(&q) != ESP_OK) return -1.0f;
  g_tx_qdbm = q;
  return q / QDBM_PER_DBM;
}

// 出力の上限を設定して読み戻す [0.25 dBm]。失敗なら理由を返す（成功なら nullptr）。
static const char *set_tx_qdbm(int8_t q) {
  const esp_err_t err = esp_wifi_set_max_tx_power(q);
  if (err != ESP_OK) return esp_err_to_name(err);
  tx_power_dbm();  // 読み戻して g_tx_qdbm を更新
  return nullptr;
}

// 自己診断: 設定したつもりの値と、実際に効いている値を並べて出す。
static void report_config() {
  uint8_t primary = 0;
  wifi_second_chan_t second = WIFI_SECOND_CHAN_NONE;
  check(esp_wifi_get_channel(&primary, &second), "esp_wifi_get_channel");

  wifi_bandwidth_t bw = WIFI_BW_HT20;
  check(esp_wifi_get_bandwidth(WIFI_IF_STA, &bw), "esp_wifi_get_bandwidth");

  uint8_t mac[6] = {};
  check(esp_wifi_get_mac(WIFI_IF_STA, mac), "esp_wifi_get_mac");

  char country[3] = {};  // 2 文字 + 3 文字目（ESP-IDF は空白を入れる）。表示は先頭 2 文字だけ
  check(esp_wifi_get_country_code(country), "esp_wifi_get_country_code");

  Serial.printf("[tx] CSI transmitter v%u commands=CH,WHO,TXP,WDTTEST,REBOOT,PAIRSET,PAIRCLEAR relay=1 "
                "watchdog_ms=%lu led_pin=%u auth=%s paired=%u "
                "rx_mac=%02x%02x%02x%02x%02x%02x tx_power_dbm=%.2f fw=%s boot=%s\n",
                PAYLOAD_VERSION, (unsigned long)WATCHDOG_MS, LED_PIN, g_auth ? "on" : "off_no_key",
                g_paired ? 1u : 0u, g_rx_mac[0], g_rx_mac[1], g_rx_mac[2], g_rx_mac[3],
                g_rx_mac[4], g_rx_mac[5], tx_power_dbm(), CSI_FW_VERSION,
                csi_boot::reset_reason_name());
  if (!g_paired) {
    Serial.printf("[tx] not_paired (set with PAIRSET / csikit flash)\n");
  }
  Serial.printf("[tx] mac=%02x%02x%02x%02x%02x%02x channel=%u (want %u) bw=%s interval_ms=%lu rate=MCS0_LGI country=%.2s\n",
                mac[0], mac[1], mac[2], mac[3], mac[4], mac[5],
                primary, WIFI_CHANNEL, bw == WIFI_BW_HT20 ? "HT20" : "HT40",
                (unsigned long)SEND_INTERVAL_MS, country);

  if (primary != WIFI_CHANNEL) {
    halt("channel verification", ESP_ERR_INVALID_STATE);
  }
}

void setup() {
  led_init();
  Serial.begin(SERIAL_BAUD);
  delay(1000);
  setup_radio();
  setup_auth();
  report_config();
#if CSI_M5_LCD
  csi_lcd_begin(CSI_LABEL, "TRANSMITTER", "fw " CSI_FW_VERSION);
  g_buttons.begin();
  Serial.printf("[tx] lcd brightness=%u\n", csi_lcd_brightness());
#endif
  Serial.println("[tx] sending");
  csi_boot::start_loop_watchdog();  // 以後 loop が 5 秒止まると再起動する
}

// ---- シリアルの命令 ---------------------------------------------------------

// チャンネルを切り替えて読み戻す。成功なら g_channel を更新して nullptr、失敗なら理由を返す。
static const char *apply_channel(uint8_t ch) {
  const esp_err_t err = esp_wifi_set_channel(ch, WIFI_SECOND_CHAN_NONE);
  if (err != ESP_OK) return esp_err_to_name(err);
  uint8_t primary = 0;
  wifi_second_chan_t second = WIFI_SECOND_CHAN_NONE;
  if (esp_wifi_get_channel(&primary, &second) != ESP_OK || primary != ch) return "verify_failed";
  g_channel = primary;
  return nullptr;
}

// シリアルの CH 命令。失敗しても止めず、理由を返す。
static void command_channel(const char *arg) {
  char *end = nullptr;
  const long ch = strtol(arg, &end, 10);
  if (end == arg || *end != '\0' || ch < CHANNEL_MIN || ch > CHANNEL_MAX) {
    Serial.printf("[tx] err cmd=CH reason=out_of_range arg=%s min=%u max=%u\n", arg,
                  CHANNEL_MIN, CHANNEL_MAX);
    return;
  }
  const char *reason = apply_channel(static_cast<uint8_t>(ch));
  if (reason != nullptr) {
    Serial.printf("[tx] err cmd=CH reason=%s channel=%ld\n", reason, ch);
    return;
  }
  g_last_heard_ms = millis();  // 番犬の猶予（受信機が追いつくまで戻らないように）
  led_start(LED_SWITCH, LED_EVENT);
  Serial.printf("[tx] ack cmd=CH channel=%u\n", g_channel);
}

// 受信機の命令を処理する: 了解を今のチャンネルで返してから切り替える。
static void handle_relay() {
  if (!g_ctrl_pending) return;
  const uint8_t ch = g_ctrl_channel;
  const uint32_t token = g_ctrl_token;
  const bool valid = ch >= CHANNEL_MIN && ch <= CHANNEL_MAX;
  CsiCtrl reply = {CTRL_MAGIC, CTRL_VERSION, static_cast<uint8_t>(valid ? CTRL_ACK : CTRL_NACK),
                   ch, token, g_nonce, {}};
  csi_auth::sign(g_key, reinterpret_cast<const uint8_t *>(&reply), CTRL_SIGNED_LEN, reply.tag);
  for (uint32_t i = 0; i < ACK_REPEAT; i++) {
    esp_now_send(BROADCAST_ADDR, reinterpret_cast<const uint8_t *>(&reply), sizeof(reply));
    delay(ACK_INTERVAL_MS);
  }
  rotate_nonce();  // この命令（と同じ合言葉の録音）はもう通らない
  const char *reason = valid ? apply_channel(ch) : "out_of_range";
  Serial.printf("[tx] relay cmd=CH channel=%u token=%lu result=%s\n", ch, (unsigned long)token,
                reason == nullptr ? "ok" : reason);
  if (reason == nullptr) {
    led_start(LED_SWITCH, LED_EVENT);
  } else {
    led_start(LED_ALERT, LED_ERROR);
  }
  g_ctrl_pending = false;
}

// 署名した制御パケットを ACK_REPEAT 回ブロードキャストする（ブロードキャストは再送されないため）。
static void send_signed_reply(uint8_t cmd, uint8_t value, uint32_t token) {
  CsiCtrl reply = {CTRL_MAGIC, CTRL_VERSION, cmd, value, token, g_nonce, {}};
  csi_auth::sign(g_key, reinterpret_cast<const uint8_t *>(&reply), CTRL_SIGNED_LEN, reply.tag);
  for (uint32_t i = 0; i < ACK_REPEAT; i++) {
    esp_now_send(BROADCAST_ADDR, reinterpret_cast<const uint8_t *>(&reply), sizeof(reply));
    delay(ACK_INTERVAL_MS);
  }
}

// 受信機の出力の命令を処理する: 変えて、変えた出力で了解を返し、合言葉を変えて試しに入る。
static void handle_txp() {
  if (g_txp_pending) {
    const uint8_t q = g_txp_qdbm;
    const uint32_t token = g_txp_token;
    const int min_q = lroundf(TX_POWER_MIN_DBM * QDBM_PER_DBM);
    const int max_q = lroundf(TX_POWER_MAX_DBM * QDBM_PER_DBM);
    if (q < min_q || q > max_q) {
      send_signed_reply(CTRL_NACK, q, token);
      rotate_nonce();
      Serial.printf("[tx] relay cmd=TXP qdbm=%u token=%lu result=out_of_range\n", q,
                    (unsigned long)token);
      led_start(LED_ALERT, LED_ERROR);
    } else {
      const int8_t prev = g_txp_trial ? g_txp_prev_qdbm : g_tx_qdbm;  // 試し中なら試しの前の値に戻す
      const char *reason = set_tx_qdbm(static_cast<int8_t>(q));
      if (reason != nullptr) {
        send_signed_reply(CTRL_NACK, q, token);
        rotate_nonce();
        Serial.printf("[tx] relay cmd=TXP qdbm=%u token=%lu result=%s\n", q,
                      (unsigned long)token, reason);
        led_start(LED_ALERT, LED_ERROR);
      } else {
        send_signed_reply(CTRL_TXP_ACK, static_cast<uint8_t>(g_tx_qdbm), token);
        rotate_nonce();  // 確認は新しい合言葉で（新しい出力の電波が届いた証拠になる）
        g_txp_prev_qdbm = prev;
        g_txp_trial_token = token;
        g_txp_confirmed = false;
        g_txp_deadline_ms = millis() + TXP_CONFIRM_MS;
        g_txp_trial = true;
        Serial.printf("[tx] relay cmd=TXP want_qdbm=%u dbm=%.2f token=%lu result=trial\n", q,
                      g_tx_qdbm / QDBM_PER_DBM, (unsigned long)token);
        led_start(LED_SWITCH, LED_EVENT);
      }
    }
    g_txp_pending = false;
  }
  if (!g_txp_trial) return;
  if (g_txp_confirmed) {
    g_txp_trial = false;
    Serial.printf("[tx] txp confirmed dbm=%.2f\n", g_tx_qdbm / QDBM_PER_DBM);
  } else if (static_cast<int32_t>(millis() - g_txp_deadline_ms) >= 0) {
    g_txp_trial = false;
    const char *reason = set_tx_qdbm(g_txp_prev_qdbm);
    Serial.printf("[tx] txp revert dbm=%.2f result=%s\n", g_tx_qdbm / QDBM_PER_DBM,
                  reason == nullptr ? "ok" : reason);
    led_start(LED_ALERT, LED_ERROR);
  }
}

// 受信機の再起動の命令: 了解を返してから再起動する（合言葉は起動ごとの乱数に変わる）。
static void handle_reboot() {
  if (!g_reboot_pending) return;
  send_signed_reply(CTRL_ACK, 0, g_reboot_token);
  Serial.printf("[tx] relay cmd=REBOOT token=%lu result=restarting\n",
                (unsigned long)g_reboot_token);
  Serial.flush();
  delay(REBOOT_DELAY_MS);
  ESP.restart();
}

// 番犬: 起動時のチャンネル以外で受信機の合図が途絶えたら、起動時のチャンネルに戻る。
static void check_watchdog() {
  if (g_channel == WIFI_CHANNEL) return;
  const uint32_t silent_ms = millis() - g_last_heard_ms;
  if (silent_ms < WATCHDOG_MS) return;
  const char *reason = apply_channel(WIFI_CHANNEL);
  led_start(LED_ALERT, LED_ERROR);
  Serial.printf("[tx] watchdog no_control_ms=%lu back_to=%u result=%s\n",
                (unsigned long)silent_ms, WIFI_CHANNEL, reason == nullptr ? "ok" : reason);
  g_last_heard_ms = millis();  // 戻せなかった場合に毎回出し続けないように
}

static void command_who() {
  uint8_t mac[6] = {};
  esp_wifi_get_mac(WIFI_IF_STA, mac);
  Serial.printf("[tx] who role=tx mac=%02x%02x%02x%02x%02x%02x channel=%u auth=%s "
                "tx_power_dbm=%.2f fw=%s paired=%u rx_mac=%02x%02x%02x%02x%02x%02x\n",
                mac[0], mac[1], mac[2], mac[3], mac[4], mac[5], g_channel,
                g_auth ? "on" : "off_no_key", tx_power_dbm(), CSI_FW_VERSION, g_paired ? 1u : 0u,
                g_rx_mac[0], g_rx_mac[1], g_rx_mac[2], g_rx_mac[3], g_rx_mac[4], g_rx_mac[5]);
}

// PAIRSET <受信機の MAC> <鍵>: NVS に置いて再起動する。応答に鍵は出さない。
static void command_pair_set(char *arg) {
  const size_t len = strlen(arg);  // store が区切りに NUL を入れるので、先に測る
  const char *reason = csi_pair::store(arg);
  memset(arg, 0, len);  // 命令の受け口に鍵を残さない
  if (reason != nullptr) {
    Serial.printf("[tx] err cmd=PAIRSET reason=%s\n", reason);
    return;
  }
  Serial.printf("[tx] ack cmd=PAIRSET restarting\n");
  csi_pair::restart();
}

static void command_pair_clear() {
  const char *reason = csi_pair::clear();
  if (reason != nullptr) {
    Serial.printf("[tx] err cmd=PAIRCLEAR reason=%s\n", reason);
    return;
  }
  Serial.printf("[tx] ack cmd=PAIRCLEAR restarting\n");
  csi_pair::restart();
}

// シリアルの TXP 命令: 送信出力の上限を変えて読み戻す。
static void command_tx_power(const char *arg) {
  char *end = nullptr;
  const float dbm = strtof(arg, &end);
  if (end == arg || *end != '\0' || dbm < TX_POWER_MIN_DBM || dbm > TX_POWER_MAX_DBM) {
    Serial.printf("[tx] err cmd=TXP reason=out_of_range arg=%s min=%.0f max=%.0f\n", arg,
                  TX_POWER_MIN_DBM, TX_POWER_MAX_DBM);
    return;
  }
  const char *reason = set_tx_qdbm(static_cast<int8_t>(lroundf(dbm * QDBM_PER_DBM)));
  if (reason != nullptr) {
    Serial.printf("[tx] err cmd=TXP reason=%s dbm=%.2f\n", reason, dbm);
    return;
  }
  g_txp_trial = false;  // シリアルで決めた値を優先する（試し中なら打ち切る）
  // 実際の値は段階に丸められる（ESP32 は 2〜20 dBm のいくつかの段階）
  Serial.printf("[tx] ack cmd=TXP want_dbm=%.2f dbm=%.2f\n", dbm, tx_power_dbm());
}

static void handle_command(char *line) {
  if (strncmp(line, "CH ", 3) == 0) {
    command_channel(line + 3);
  } else if (strncmp(line, "TXP ", 4) == 0) {
    command_tx_power(line + 4);
  } else if (strcmp(line, "REBOOT") == 0) {
    Serial.printf("[tx] ack cmd=REBOOT restarting\n");
    Serial.flush();
    delay(REBOOT_DELAY_MS);
    ESP.restart();
  } else if (strcmp(line, "WDTTEST") == 0) {
    Serial.printf("[tx] wdt_test block_ms=%lu (watchdog=%s)\n", (unsigned long)WDT_TEST_BLOCK_MS,
                  csi_boot::watchdog_on() ? "on" : "off");
    Serial.flush();
    delay(WDT_TEST_BLOCK_MS);  // ウォッチドッグが働けば、ここで再起動する
    Serial.printf("[tx] wdt_test survived (watchdog did not fire)\n");
  } else if (strcmp(line, "WHO") == 0) {
    command_who();
  } else if (strncmp(line, "PAIRSET ", 8) == 0) {
    command_pair_set(line + 8);
  } else if (strcmp(line, "PAIRCLEAR") == 0) {
    command_pair_clear();
  } else {
    Serial.printf("[tx] err cmd=%s reason=unknown\n", line);
  }
}

// シリアルに届いた分だけ読み、改行で 1 命令にする（待たない）。
static void poll_commands() {
  static char s_buf[COMMAND_MAX_LEN + 1];
  static size_t s_len = 0;
  static bool s_overflow = false;
  while (Serial.available() > 0) {
    const int c = Serial.read();
    if (c == '\r') continue;
    if (c == '\n') {
      s_buf[s_len] = '\0';
      if (s_overflow) {
        Serial.printf("[tx] err cmd=? reason=too_long max=%u\n", (unsigned)COMMAND_MAX_LEN);
      } else if (s_len > 0) {
        handle_command(s_buf);
      }
      s_len = 0;
      s_overflow = false;
    } else if (s_len < COMMAND_MAX_LEN) {
      s_buf[s_len++] = static_cast<char>(c);
    } else {
      s_overflow = true;
    }
  }
}

// ---- 液晶の表示（M5Stack のみ） ---------------------------------------------

#if CSI_M5_LCD
// 1 秒ごとの状況を液晶に渡す。送信エラーが増えた = 赤、レート低下・受信機と通じていない = 黄。
// ctrl_rx は受信機の合図（KEEPALIVE）で 1 秒ごとに増えるので、相手が生きているかが分かる
static void post_lcd(uint32_t seq, float rate_hz, uint32_t send_err, uint32_t err_delta,
                     uint32_t cb_fail) {
  const uint32_t now = millis();
  const uint32_t heard = g_last_heard_ms;
  const bool peer_seen = g_ctrl_rx > 0 && now - heard < LCD_PEER_LOST_MS;
  CsiLcdStatus st = {};
  if (err_delta > 0) {
    st.level = CSI_LCD_ERROR;
    snprintf(st.state, sizeof(st.state), "SEND ERROR");
  } else if (!g_paired) {
    st.level = CSI_LCD_WARN;  // 送信は続けるが、受信機の命令を受け付けない（csikit flash で組む）
    snprintf(st.state, sizeof(st.state), "NOT PAIRED");
  } else if (rate_hz < LCD_RATE_WARN_HZ || !peer_seen) {
    st.level = CSI_LCD_WARN;
    snprintf(st.state, sizeof(st.state), rate_hz < LCD_RATE_WARN_HZ ? "LOW RATE" : "NO %s",
             CSI_PEER_LABEL);
  } else {
    st.level = CSI_LCD_OK;
    snprintf(st.state, sizeof(st.state), "SENDING");
  }
  uint8_t mac[6] = {};
  esp_wifi_get_mac(WIFI_IF_STA, mac);
  snprintf(st.lines[0], sizeof(st.lines[0]), "%02x:%02x:%02x:%02x:%02x:%02x", mac[0], mac[1],
           mac[2], mac[3], mac[4], mac[5]);
  snprintf(st.lines[1], sizeof(st.lines[1]), "CH %u   %.1f Hz", g_channel, rate_hz);
  snprintf(st.lines[2], sizeof(st.lines[2]), "TXP %.1f dBm%s", g_tx_qdbm / QDBM_PER_DBM,
           g_txp_trial ? "  TRIAL" : "");
  snprintf(st.lines[3], sizeof(st.lines[3]), "seq %lu  err %lu/%lu", (unsigned long)seq,
           (unsigned long)send_err, (unsigned long)cb_fail);
  if (g_ctrl_rx == 0) {
    snprintf(st.lines[4], sizeof(st.lines[4]), "%s: not heard", CSI_PEER_LABEL);
  } else {
    snprintf(st.lines[4], sizeof(st.lines[4]), "%s: %.1f s ago", CSI_PEER_LABEL,
             (now - heard) / 1000.0f);
  }
  csi_lcd_format_uptime(st.lines[5], sizeof(st.lines[5]), now);
  csi_lcd_post(st);
}
#endif

// ---- 送信ループ -----------------------------------------------------------

void loop() {
  // vTaskDelayUntil は「前回の起床時刻 + 間隔」まで待つので、送信処理の時間で間隔がずれない
  static TickType_t s_last_wake = xTaskGetTickCount();
  static uint32_t s_seq = 0;
  static uint32_t s_send_err = 0;      // esp_now_send 自体が拒否した回数（キュー溢れ等）
  static esp_err_t s_last_err = ESP_OK;
  static uint32_t s_last_report_ms = millis();
  static uint32_t s_seq_at_report = 0;

  vTaskDelayUntil(&s_last_wake, pdMS_TO_TICKS(SEND_INTERVAL_MS));

  CsiTxPayload payload = {};
  payload.magic = PAYLOAD_MAGIC;
  payload.version = PAYLOAD_VERSION;
  payload.interval_ms = static_cast<uint16_t>(SEND_INTERVAL_MS);
  payload.seq = s_seq;
  payload.tx_ms = millis();
  payload.ctrl_nonce = g_nonce;
  payload.tx_qdbm = g_tx_qdbm;
  static int8_t s_temp_c = 0;
  static uint32_t s_temp_ms = 0;
  if (s_temp_ms == 0 || millis() - s_temp_ms >= TEMP_INTERVAL_MS) {
    const long t = lroundf(temperatureRead());
    s_temp_c = static_cast<int8_t>(t < INT8_MIN ? INT8_MIN : (t > INT8_MAX ? INT8_MAX : t));
    s_temp_ms = millis();
  }
  payload.tx_temp_c = s_temp_c;

  esp_err_t err = esp_now_send(BROADCAST_ADDR, reinterpret_cast<const uint8_t *>(&payload),
                               sizeof(payload));
  if (err != ESP_OK) {
    s_send_err++;
    s_last_err = err;
    led_start(LED_ALERT, LED_ERROR);
  } else if (payload.seq % LED_SYNC_EVERY == 0) {
    led_start(LED_BEAT_TX, LED_NORMAL);  // 受信機も同じ連番を受けた瞬間に光る
  }
  s_seq++;  // 失敗しても進める。受信側では欠番として見える
  led_update();

  poll_commands();
  handle_relay();
  handle_txp();
  handle_reboot();
  check_watchdog();
#if CSI_M5_LCD
  const csi_buttons::Action action = g_buttons.poll();
  if (action == csi_buttons::Action::BRIGHTNESS || action == csi_buttons::Action::WAKE) {
    Serial.printf("[tx] lcd brightness=%u source=button\n", csi_lcd_brightness());
  }
#endif

  const uint32_t now_ms = millis();
  if (now_ms - s_last_report_ms >= REPORT_INTERVAL_MS) {
    const uint32_t sent = s_seq - s_seq_at_report;
    const float rate_hz = sent * 1000.0f / (now_ms - s_last_report_ms);
    Serial.printf("[tx] seq=%lu rate_hz=%.1f cb_ok=%lu cb_fail=%lu send_err=%lu last_err=%s "
                  "channel=%u ctrl_rx=%lu ctrl_bad=%lu tx_power_dbm=%.2f%s\n",
                  (unsigned long)s_seq, rate_hz, (unsigned long)g_cb_ok,
                  (unsigned long)g_cb_fail, (unsigned long)s_send_err,
                  esp_err_to_name(s_last_err), g_channel, (unsigned long)g_ctrl_rx,
                  (unsigned long)g_ctrl_bad, g_tx_qdbm / QDBM_PER_DBM,
                  g_txp_trial ? " txp_trial=1" : "");
#if CSI_M5_LCD
    static uint32_t s_err_at_report = 0;
    post_lcd(s_seq, rate_hz, s_send_err, s_send_err - s_err_at_report, g_cb_fail);
    s_err_at_report = s_send_err;
#endif
    s_last_report_ms = now_ms;
    s_seq_at_report = s_seq;
  }
}
