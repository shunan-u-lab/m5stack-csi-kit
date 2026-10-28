// CSI 受信機: ルーターに接続せず、固定チャンネルで送信機（firmware/csi-tx）の
// パケットだけを拾い、その CSI をシリアルへ 1 行ずつ出す。
//
// - ルーター不要。チャンネルがルーター都合でずれない（Wi-Fi を設定してあれば起動時に一度だけ手元の Wi-Fi で時刻を取り、
//   すぐ切断してから受信を始める。下の「時刻」）
// - 送信元 MAC で送信機のパケットだけに絞る（周囲のビーコン等は数えるだけで出さない）
// - CSI コールバックは Wi-Fi タスクで走る。そこでシリアルに書くと 100 Hz では詰まるため、
//   キューに積んで別タスクで書き出す。キューが溢れたら捨てて数える（黙って落とさない）
// - 1 行 = 16 進の CSI + CRC32。USB 経由の転送で壊れた行を受信側で検出できる
// - 送信機の連番（seq）を CSI 行に付ける。CSI コールバックには中身が渡らないため、
//   プロミスキャス受信で中身を読み、受信時刻（rx_ctrl.timestamp）で CSI と対応づける。
//   複数の受信機の行を、同じ送信パケット同士で揃えるのに使う
// - 内蔵 LED（GPIO2）で動作を示す: 1 秒に 2 回の点滅 = 送信機のパケットを受信中（送信機は 1 回。
//   送信機と同じパケットで光るので 2 台がほぼ同時に光る）。受信なし = 2 秒ごとに短くちらつく。
//   切り替え = 素早く 3 回、番犬・失敗 = 1 秒の速い点滅、初期化失敗 = 速い点滅が続く
// - シリアルの命令でチャンネルを切り替えられる（チャンネルの掃引など）。起動時は WIFI_CHANNEL。
//     CH <1-13>  チャンネルを切り替え、読み戻して確かめる → # ack cmd=CH channel=<n>
//     WHO        役割・MAC・今のチャンネルを返す（再起動せずにポートを見分ける）
//     BATT       電源 IC（IP5306）の充電の状態（M5Stack のみ・firmware/shared/CsiLcd/src/CsiPower.h）
//   失敗は # err cmd=<命令> reason=<理由> を返し、受信は止めない。応答はすべて '#' 行
//   （取得側では注記として記録に残る）
// - CH は送信機にも無線（ESP-NOW）で伝える（送信機を PC から離して電源だけで使うため）:
//   送信機に SET_CH を送り → 了解を待ち → 自分も切り替え → 送信機のパケットが新しいチャンネルで
//   届くのを確かめてから ack を返す。自分だけ切り替えるのは CHLOCAL <1-13>。
//   OTHERS は送信機以外から来た CSI を送信元ごとに数えた表を出してリセットする（2 台目の送信機を調べる用）
//   BSCAN [dwell_ms] は ch1〜13 を自分で回り、聞こえたビーコンを BSSID ごとに出して元のチャンネルに戻る
//   ALLCSI ON|OFF は送信機以外から来た CSI も出す（テスト用。番犬を止める。SD の記録中は不可）
//   1 秒ごとに送信機へ合図（KEEPALIVE）を送る。番犬: 起動時のチャンネル以外で送信機のパケットが
//   WATCHDOG_MS 途絶えたら起動時のチャンネルに戻る（送信機も合図が途絶えると戻るので、両方が集まる）
// - 制御パケット（SET_CH・KEEPALIVE と送信機の了解）は共有の鍵で署名する（firmware/shared/CsiAuth・
//   2026-10-07）。送信機がデータのパケットに載せる合言葉（ctrl_nonce）を読み、それを含めて署名する。
//   組（PAIRSET）が未設定なら送信機に無線で命令しない（CH は err reason=no_key。CHLOCAL は使える）。
//   合言葉をまだ読めていなければ reason=no_nonce。署名の合わない了解は ctrl_bad に数えて捨てる
// - 送信の出力: 起動時は CSI_TX_POWER_QDBM（0.25 dBm 単位。0 = ESP-IDF の既定のまま）。
//     TXP <2-20>  出力の上限 [dBm] を変えて読み戻す → # ack cmd=TXP dbm=<実際の値>
//   受信機が送るのは制御パケット（1 秒に 1 回の合図）だけ
// - 送信機の出力を無線で変える（2026-10-07）:
//     TXPS <2-20>  送信機の出力の上限 [dBm] を変える → # ack cmd=TXPS want_dbm=<指定> dbm=<実際の値>
//   送信機に SET_TXP を送り → 了解（実際の値）を待ち → 新しい出力の電波（送信機の新しい合言葉）が届くのを
//   確かめてから確認（TXP_CONFIRM）を送る。届かなければ # err cmd=TXPS reason=lost_link（送信機は
//   TXP_CONFIRM_MS 後に自分で元の出力に戻る。戻ったかも確かめて reverted=1/0 を付ける）
// - 送信機の今の出力は、データのパケット（版 3 の tx_qdbm）から読み、液晶と # stats の peer_tx_power_dbm に出す
// - 見張りと再起動（2026-10-07）: loop が 5 秒止まると自動で再起動する（ウォッチドッグ。firmware/shared/CsiBoot）。
//   loop の中の長い待ちは csi_boot::wait_ms（待つ間も知らせる）。起動時の表示に前回の再起動の理由（boot=）。
//     TXREBOOT  送信機を無線で再起動する（署名付き）→ # ack cmd=TXREBOOT acked=<0/1> back=<0/1>
//               （back: 送信機の連番が 0 近くに戻ってまた届いた）
//     WDTTEST   わざと loop を止めてウォッチドッグを確かめる（5 秒ほどで再起動する）
//     REBOOT    受信機自身を再起動する（USB の DTR/RTS でリセットできない変換チップ用。次の起動は boot=sw）
//   送信機の連番が大きく戻ったら送信機が再起動したとみなし、# stats の tx_restarts に数える（記録に残る）
// - SD カードへの保存（CSI_SD=1。設計は docs/decisions/0006）:
//   既定は書き込みなし。ボタン A = 新しいセッションを始める、B = 止める、C = 印（# mark label=button）。
//   印を付けると M5Stack は短く鳴り（記録中でなければ低く長く）、状態の帯が 2 秒「MARK n OK」になる（2026-10-11）。
//   セッションごとに /CSI/S0001.csv。中身はシリアルの出力と同じ（CSI 行と # 行）。液晶の 6 行目に SD の状態。
//   長いセッションは 1 時間ごとに次の通し番号のファイルに切り替える（2026-10-09。FAT32 の 4 GiB の上限と、
//   壊れたときの被害を小さくするため）。前のファイルの最後に # sd rotate、次のファイルの先頭に見出し
//   （# capture start・# sd session source=rotate prev=… part=…・受信機と送信機の行）を書く。
//   液晶の 6 行目は「今のファイル名・その量・セッション全体の量」（例: S0003 45.1M all 1.23G）
//     SDSTART / SDSTOP / SDMARK [ラベル]   ボタンと同じことをシリアルから
//     SDLS                 /CSI のファイルの一覧 → # sd file name=S0001.csv bytes=…
//     SDGET <名前> [位置]  ファイルを送る（#SDC <位置> <長さ> <crc32> の行と中身を繰り返す。セッション外だけ。
//                          送る間は CSI 行のシリアルへの出力を止める）。PC 側は scripts/sd_fetch.py
//     SDRM <名前>          ファイルを消す（セッション外だけ）→ # ack cmd=SDRM name=… bytes=… free_before_mb=…
//                          free_after_mb=… freed_mb=…（消す前後の空き。2026-10-10）
//     SDINFO               SD の状態
//     SDROTATE <秒>        ファイルを切り替える間隔を変える（試験用。0 = 切り替えない。再起動で 1 時間に戻る）
//     SDEVENTS [CLEAR]     NVS に残した SD の出来事（書き込みの失敗・再開）→ # sdevent …（CLEAR で読んだ後に消す）
//     SDFAULT <n>          試験用: 次の n 回のドライバの書き込みをわざと失敗させる（0〜10。書き直しと
//                          立て直しを確かめる。書き直しは 1 回の書き込みで最大 3 回なので、4 以上で立て直しになる）
//     TIME <unix 秒> [UTC からの差 秒]  時刻を教える（セッションの先頭に # capture start=… を書く）
// - PC なしの夜間の記録（2026-10-07）:
//   時刻: 起動時に手元の Wi-Fi へつなぎ、NTP で時刻を取って切断してから受信を始める（# ntp ok=1/0）。
//     Wi-Fi の名前とパスワードはファームウェアに埋め込まず、受信機の NVS に置く（sd_fetch.py wifi）。
//     WIFISET <名前の16進> <パスワードの16進> <UTC からの差 秒>  設定する（応答に中身は出さない）
//     WIFICLEAR            消す（借用機材を返す前など）
//   自動再開: セッション中に再起動した（停電・ウォッチドッグ・リセット）ら、起動後に新しいセッションを
//     始める（source=resume。見出しの boot= に理由）。ボタン B・SDSTOP で止めたときだけ再開しない
//   画面: ボタン C を 1 秒押し続けると明るさが 普通 → 暗い → 消灯 → 普通 と変わる（覚えておく）。
//     消灯中は、どのボタンも最初の 1 回は画面を点けるだけ（暗い中で押し間違えて止めないように）

#include <Arduino.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <stddef.h>
#include <sys/time.h>

#include <CsiAuth.h>
#include <CsiBoot.h>
#include <CsiPair.h>

#include "sd_log.h"
#include <Preferences.h>
#include <esp_sntp.h>
#include <freertos/queue.h>
#include <string.h>

// M5Stack の液晶に稼働状況を出すか（1 = 出す。platformio.ini の M5Stack 用 env だけで 1 にする）
#ifndef CSI_M5_LCD
#define CSI_M5_LCD 0
#endif
#if CSI_M5_LCD
#include <CsiButtons.h>
#include <CsiLcd.h>
#include <CsiPower.h>
#endif
// 液晶に出す機器番号と送信機の番号（M5Stack 用 env の build_flags で与える）
#ifndef CSI_LABEL
#define CSI_LABEL "R1"
#endif
#ifndef CSI_PEER_LABEL
#define CSI_PEER_LABEL "T1"
#endif

// ファームウェアの版（platformio.ini が firmware/shared/fw_version.py で与える。例 "1007-db82545"）
#ifndef CSI_FW_VERSION
#define CSI_FW_VERSION "unknown"
#endif
// 起動時の送信出力の上限 [0.25 dBm]（esp_wifi_set_max_tx_power の単位）。0 = ESP-IDF の既定のまま
#ifndef CSI_TX_POWER_QDBM
#define CSI_TX_POWER_QDBM 0
#endif

// ---- 設定（送信機と必ず揃える） ------------------------------------------

// 起動時の受信チャンネル（送信機の WIFI_CHANNEL と同じ）。CH 命令で変えられる（電源を切ると戻る）
constexpr uint8_t WIFI_CHANNEL = 11;
// CH 命令で受け付ける範囲。日本では ch1〜13 が OFDM で使える（ch14 は DSSS のみで 11n は不可）
constexpr uint8_t CHANNEL_MIN = 1;
constexpr uint8_t CHANNEL_MAX = 13;
// 国の設定。既定（"CN"）のままにせず明示する。802.11d は使わない（AP に接続しないため）
constexpr const char *COUNTRY_CODE = "JP";
// 命令 1 行の最大長 [byte]（改行を除く）。超えたら捨てて err を返す
constexpr size_t COMMAND_MAX_LEN = 232;  // WIFISET（名前 32 バイト・パスワード 63 バイトを 16 進で）が入る長さ
// 命令を確かめる間隔 [ms]
constexpr uint32_t COMMAND_POLL_MS = 10;
// 送信機への命令: 了解を待つ時間 [ms] と送る回数
constexpr uint32_t RELAY_ACK_TIMEOUT_MS = 300;
constexpr uint32_t RELAY_ATTEMPTS = 3;
// 切り替えの後、送信機のパケットが届くかを見る時間 [ms] と、届いたとみなす数
constexpr uint32_t LINK_CHECK_MS = 500;
constexpr uint32_t LINK_MIN_FRAMES = 5;  // 100 Hz なら 0.05 秒分
// 送信機への合図の間隔 [ms]（送信機の番犬 4 秒より十分短く）
constexpr uint32_t KEEPALIVE_MS = 1000;
// 番犬: 起動時のチャンネル以外で、送信機のパケットがこれだけ途絶えたら戻る [ms]
constexpr uint32_t WATCHDOG_MS = 5000;
// 送信機の STA MAC。起動時に NVS から読む（PAIRSET で置く。firmware/shared/CsiPair）。
// 未設定なら全部 0 のままで、どの送信機のパケットも拾わない（液晶とシリアルに not_paired）
static uint8_t g_tx_mac[6] = {};
static bool g_paired = false;
// Pico W の AP に接続して命令を中継するか（1 = する）。キットは M5Stack Basic 2 台だけなので既定は 0
// （docs/decisions/0001。Pico W 向けのコードは実機で確かめてから外す）。0 でも Pico W の CSI は受ける
#ifndef CSI_PICO_RELAY
#define CSI_PICO_RELAY 0
#endif
constexpr bool PICO_RELAY = CSI_PICO_RELAY != 0;
// 2 台目の送信機（Raspberry Pi Pico W・firmware/csi-tx-picow。2026-09-23 実測）。ESP-NOW ではなく、
// 暗号なしの AP の UDP ブロードキャスト（データフレーム・OFDM 6 Mbps）を 100 Hz で出す
constexpr uint8_t TX2_MAC[6] = {0x02, 0x00, 0x00, 0x00, 0x00, 0x0c};
constexpr uint32_t PICO_MAGIC = 0x50495343;  // "CSIP"（Pico W のペイロードの識別子）
// 受け付ける送信機の一覧（番号 = 並び）。番号 0 が主の送信機（命令・合図・番犬の相手）
constexpr size_t N_TX = 2;
static const uint8_t *const TX_LIST[N_TX] = {g_tx_mac, TX2_MAC};
// 送信機の機種名（見出しの name= に出す。空白なしの英数字。画面の表示に使う）
static const char *const TX_NAMES[N_TX] = {"M5Stack-Basic", "PicoW"};

// Pico W を PC から離して無線で切り替えるため、Pico W の AP に STA として接続し、UDP で命令（CH）と
// 合図（KA）を送る（2026-09-23）。チャンネルを変える時は切断 → 切り替え → 接続し直す
constexpr const char *PICO_SSID = "csi-picow";  // firmware/csi-tx-picow の AP_SSID（暗号なし）
constexpr uint16_t PICO_CMD_PORT = 5006;       // Pico W が命令を受けるポート
constexpr uint16_t PICO_REPLY_PORT = 5007;     // Pico W の返事を受けるポート（自分側）
// 接続を待つ時間 [ms]。実測は 40〜300 ms。長いと命令と S への合図が止まる（S の番犬は 4 秒）
constexpr uint32_t PICO_JOIN_TIMEOUT_MS = 1000;
constexpr uint32_t PICO_REPLY_TIMEOUT_MS = 150;  // Pico W の ack を待つ時間 [ms]（実測 13〜28 ms）
constexpr uint32_t PICO_ATTEMPTS = 3;            // "CH" を送る回数
constexpr uint32_t PICO_KEEPALIVE_MS = 1000;     // 合図（KA）の間隔 [ms]（Pico W の番犬 4 秒より短く）
constexpr uint32_t PICO_REJOIN_MS = 3000;        // 接続していない時に試す間隔 [ms]
constexpr uint32_t PICO_VISIBLE_MS = 1000;       // この間に Pico W のフレームを見た時だけ試す [ms]
// 最後に話してからこれだけたてば、Pico W は番犬（合図が 4 秒途絶）で起動時のチャンネルに戻ったとみなす [ms]
constexpr uint32_t PICO_WATCHDOG_GUESS_MS = 4000;
static const IPAddress PICO_AP_IP(192, 168, 4, 1);   // Pico W の AP（arduino-pico の既定）
static const IPAddress PICO_STA_IP(192, 168, 4, 50); // 自分（固定。DHCP を待たない）
static const IPAddress PICO_NETMASK(255, 255, 255, 0);

// 送信元の MAC が一覧のどれか（番号）。どれでもなければ -1
static int tx_index(const uint8_t *mac) {
  for (size_t i = 0; i < N_TX; i++) {
    if (memcmp(mac, TX_LIST[i], 6) == 0) return static_cast<int>(i);
  }
  return -1;
}

// シリアル速度 [bit/s]。100 Hz × 約 600 バイト/行 ≒ 60 KB/s を流すため
constexpr uint32_t SERIAL_BAUD = 921600;
// シリアル送信バッファ [byte]。書き出しタスクがブロックする時間を減らす
constexpr size_t SERIAL_TX_BUFFER = 8192;
// CSI 生データの最大長 [byte]。LLTF + HT-LTF + STBC-HT-LTF（各 64 サブキャリア × 2 byte）= 384
constexpr uint16_t CSI_MAX_LEN = 384;
// キューの深さ [件]。100 Hz で 0.32 秒分
constexpr UBaseType_t QUEUE_DEPTH = 32;
// 状況報告の間隔 [ms]
constexpr uint32_t REPORT_INTERVAL_MS = 1000;
// 初期化失敗時に理由を出し直す間隔 [ms]
constexpr uint32_t HALT_REPEAT_MS = 2000;
// 書き出しタスクのスタック [byte] と優先度
constexpr uint32_t WRITER_STACK = 4096;
constexpr UBaseType_t WRITER_PRIORITY = 1;
// 出力形式の版。列を変えたら上げる（2: rx_us の次に seq を追加 / 3: 送信元と区間の列を追加し、
// CSI は 1 区間だけ出す。2 台 × 100 Hz をシリアル 921600 bps に収めるため）
constexpr uint16_t FORMAT_VERSION = 3;
// 1 区間（64 サブキャリア × [虚部, 実部]）のバイト数
constexpr uint16_t LTF_BYTES = 128;

// ---- 送信機のペイロード（firmware/csi-tx の CsiTxPayload と同じ） -----------------

constexpr uint32_t PAYLOAD_MAGIC = 0x54495343;  // "CSIT"

// 制御パケット（firmware/csi-tx の CsiCtrl と同じ。リトルエンディアン・24 バイト）
constexpr uint32_t CTRL_MAGIC = 0x43495343;  // "CSIC"
constexpr uint16_t CTRL_VERSION = 2;  // 2: 合言葉と署名を足した（2026-10-07）
enum CtrlCmd : uint8_t {
  CTRL_SET_CH = 1, CTRL_ACK = 2, CTRL_KEEPALIVE = 3, CTRL_NACK = 4,
  CTRL_SET_TXP = 5, CTRL_TXP_ACK = 6, CTRL_TXP_CONFIRM = 7,  // 出力の変更（channel の欄に 0.25 dBm 単位）
  CTRL_REBOOT = 8,  // 送信機を再起動する（了解 ACK の後に再起動）
};
// 送信機の再起動を待つ時間 [ms]（起動に約 1〜2 秒。連番が戻ってまた届くまで）
constexpr uint32_t TX_REBOOT_WAIT_MS = 6000;
// WDTTEST で loop を止める時間 [ms]（ウォッチドッグの 5 秒より長く）
constexpr uint32_t WDT_TEST_BLOCK_MS = 10000;
constexpr uint32_t REBOOT_DELAY_MS = 50;  // REBOOT の応答を出し切ってから再起動するまで [ms]
// 送信機が出力の試しを終えるまでの時間 [ms]（firmware/csi-tx の TXP_CONFIRM_MS と同じ）
constexpr uint32_t TXP_CONFIRM_MS = 3000;
// 新しい出力の電波（新しい合言葉）を待つ時間 [ms] と、その間に要るフレーム数
constexpr uint32_t TXP_LINK_WAIT_MS = 1500;
constexpr uint32_t TXP_LINK_MIN_FRAMES = 5;
constexpr uint32_t TXP_CONFIRM_REPEAT = 3;     // 確認を送る回数（届かなければ送信機は元に戻る）
constexpr uint32_t TXP_CONFIRM_INTERVAL_MS = 5;
constexpr uint32_t TXP_REVERT_MARGIN_MS = 1500;  // 送信機が戻ったか確かめるまでの余裕 [ms]
struct __attribute__((packed)) CsiCtrl {
  uint32_t magic;
  uint16_t version;
  uint8_t cmd;
  uint8_t channel;
  uint32_t token;
  uint32_t nonce;                  // 送信機の合言葉（データのパケットの ctrl_nonce）
  uint8_t tag[csi_auth::TAG_LEN];  // 署名（先頭 CTRL_SIGNED_LEN バイトに対して）
};
constexpr size_t CTRL_SIGNED_LEN = offsetof(CsiCtrl, tag);
// 送信機のペイロードの版 2 以降は、先頭 16 バイト（CsiTxPayload）の後に合言葉（uint32）が続く。
// Pico W は版 1 のままなので、CsiTxPayload は 16 バイトのまま変えない
constexpr uint16_t PAYLOAD_VERSION_NONCE = 2;
constexpr size_t PAYLOAD_NONCE_OFFSET = 16;
// 版 3 以降は、その後に送信機の出力（int8・0.25 dBm 単位）が続く
constexpr uint16_t PAYLOAD_VERSION_TXP = 3;
constexpr size_t PAYLOAD_TXP_OFFSET = 20;
// 版 4 以降は、その後に送信機のチップの温度（int8・℃。内蔵センサーの目安）が続く（2026-10-10）
constexpr uint16_t PAYLOAD_VERSION_TEMP = 4;
constexpr size_t PAYLOAD_TEMP_OFFSET = 21;

// 送信出力の上限として受け付ける範囲 [dBm]（ESP32 の esp_wifi_set_max_tx_power は 2〜20 dBm）
constexpr float TX_POWER_MIN_DBM = 2.0f;
constexpr float TX_POWER_MAX_DBM = 20.0f;
constexpr float QDBM_PER_DBM = 4.0f;  // esp_wifi の出力の単位は 0.25 dBm
struct __attribute__((packed)) CsiTxPayload {
  uint32_t magic;
  uint16_t version;
  uint16_t interval_ms;
  uint32_t seq;
  uint32_t tx_ms;
};

// ESP-NOW のフレーム（802.11 管理フレームの Action・ベンダ固有）内の位置 [byte]
constexpr size_t FRAME_CTRL = 0;           // 0xD0 = 管理フレーム・Action
constexpr uint8_t FRAME_CTRL_ACTION = 0xD0;
constexpr size_t FRAME_ADDR2 = 10;         // 送信元 MAC
constexpr size_t FRAME_CATEGORY = 24;      // 127 = ベンダ固有
constexpr uint8_t CATEGORY_VENDOR = 127;
constexpr size_t FRAME_ELEMENT_ID = 32;    // 221 = ベンダ固有要素
constexpr uint8_t ELEMENT_VENDOR = 221;
constexpr size_t FRAME_ELEMENT_OUI = 34;   // Espressif の OUI 18:fe:34
constexpr uint8_t ESPRESSIF_OUI[3] = {0x18, 0xFE, 0x34};
constexpr size_t FRAME_ELEMENT_TYPE = 37;  // 4 = ESP-NOW
constexpr uint8_t ELEMENT_TYPE_ESPNOW = 4;
constexpr size_t FRAME_BODY = 39;          // ここから送信機のペイロード
// 起動後最初のフレームを診断用に出す長さ [byte]
constexpr size_t FIRST_FRAME_DUMP = FRAME_BODY + sizeof(CsiTxPayload);

// 連番の対応づけ
constexpr size_t SEQ_RING_SIZE = 64;               // 直近の (受信時刻, 連番)。100 Hz で 0.64 秒分
// 受信時刻の差の許容 [µs]。同じパケットなら CSI とプロミスキャスの受信時刻は完全に一致する
// （2026-09-23 実測。400 行中 399 行が差 0）。1 行だけ 1315 µs の対応があり、別パケットの
// 連番を付けた恐れがあるため、一致のみを採る。誤った連番より「不明（-1）」の方が安全
constexpr int32_t SEQ_MATCH_TOLERANCE_US = 0;
constexpr uint32_t SEQ_WAIT_RETRIES = 3;           // 見つからないとき待ち直す回数
constexpr TickType_t SEQ_WAIT_TICKS = 1;           // 待ち直す間隔 [tick]
constexpr int64_t SEQ_UNKNOWN = -1;                // 対応が取れなかった行の seq

// ---- キューに積む 1 件 -----------------------------------------------------

struct CsiRecord {
  uint32_t rx_us;          // 受信時刻 [µs]（受信機ローカル。rx_ctrl.timestamp）
  int8_t rssi_dbm;         // RSSI [dBm]
  int8_t noise_floor_dbm;  // 雑音レベル [dBm]
  uint8_t channel;         // 一次チャンネル
  uint8_t secondary;       // 二次チャンネル（0: なし / 1: 上 / 2: 下）
  uint8_t sig_mode;        // 0: 11bg / 1: HT(11n)
  uint8_t mcs;             // MCS（HT のみ有効）
  uint8_t cwb;             // 0: 20 MHz / 1: 40 MHz
  uint8_t stbc;            // 0: 非 STBC / 1: STBC
  uint8_t sgi;             // 0: Long GI / 1: Short GI
  uint8_t first_word_invalid;  // 1 なら先頭 4 byte（2 サブキャリア分）が無効
  uint16_t len;            // CSI 生データ長 [byte]
  uint8_t tx;              // 送信機の番号（TX_LIST の並び。TX_OTHER = それ以外の送信元・ALLCSI）
  uint8_t mac[6];          // 送信元の MAC（TX_OTHER のときだけ使う）
  int8_t buf[CSI_MAX_LEN]; // [虚部, 実部] の順に int8 が並ぶ
};

static QueueHandle_t g_queue = nullptr;

// ALLCSI（2026-10-14・テスト用）: オンの間は送信機以外から来た CSI も出す（<tx> に実際の送信元）。
// 番犬を止める（CHLOCAL で受信機だけ別のチャンネルに置ける）。SD の記録とは混ぜない（記録中はオンにできない）
constexpr uint8_t TX_OTHER = 0xFF;  // CsiRecord.tx: 送信機以外の送信元
static volatile bool g_all_csi = false;
static volatile uint32_t g_all_csi_rows = 0;  // ALLCSI で出した他の送信元の行（オンにするたびに 0）

struct SeqEntry {
  uint32_t rx_us;  // 受信時刻 [µs]
  uint32_t seq;    // 送信機の連番
  uint8_t tx;      // 送信機の番号
};
static SeqEntry g_seq_ring[SEQ_RING_SIZE];
static uint32_t g_seq_count = 0;  // 書き込んだ総数（リングの位置は % SEQ_RING_SIZE）
static portMUX_TYPE g_seq_mux = portMUX_INITIALIZER_UNLOCKED;

// 起動後最初の送信機フレーム（診断用）。書き出しタスクが 1 度だけ出す
static uint8_t g_first_frame[FIRST_FRAME_DUMP];
static volatile bool g_first_frame_ready = false;

// 計数（コールバックは Wi-Fi タスク、読むのは loop）
static volatile uint32_t g_matched = 0;    // 主の送信機（番号 0）からの CSI
static volatile uint32_t g_matched2 = 0;   // 2 台目の送信機（番号 1）からの CSI
static volatile uint32_t g_other = 0;      // 他の送信元からの CSI（出さない）

// 調べる用: 送信機以外から来た CSI を送信元ごとに数える（OTHERS 命令で表示してリセット）。
// 2 台目の送信機（Pico W 等）の CSI が取れているか、どの変調で届いているかを見る
constexpr size_t OTHER_SLOTS = 8;  // 数える送信元の数（あふれた分は overflow に数える）
struct OtherStat {
  uint8_t mac[6];
  uint32_t n;          // CSI の数
  uint32_t n_ht;       // うち 11n（HT）
  uint8_t last_rate;   // 最後の 11b/g の速さの符号（rx_ctrl.rate。HT では無効）
  uint8_t last_mcs;    // 最後の MCS（HT のみ）
  uint16_t last_len;   // 最後の CSI の長さ [byte]
  int32_t rssi_sum;    // RSSI の合計 [dBm]（平均を出す）
};
static OtherStat g_others[OTHER_SLOTS];
static size_t g_others_used = 0;
static uint32_t g_others_overflow = 0;
static portMUX_TYPE g_others_mux = portMUX_INITIALIZER_UNLOCKED;

static void count_other(const wifi_csi_info_t *info) {
  portENTER_CRITICAL(&g_others_mux);
  OtherStat *slot = nullptr;
  for (size_t i = 0; i < g_others_used; i++) {
    if (memcmp(g_others[i].mac, info->mac, 6) == 0) {
      slot = &g_others[i];
      break;
    }
  }
  if (slot == nullptr && g_others_used < OTHER_SLOTS) {
    slot = &g_others[g_others_used++];
    memset(slot, 0, sizeof(*slot));
    memcpy(slot->mac, info->mac, 6);
  }
  if (slot == nullptr) {
    g_others_overflow++;
  } else {
    slot->n++;
    if (info->rx_ctrl.sig_mode == 1) slot->n_ht++;
    slot->last_rate = info->rx_ctrl.rate;
    slot->last_mcs = info->rx_ctrl.mcs;
    slot->last_len = info->len;
    slot->rssi_sum += info->rx_ctrl.rssi;
  }
  portEXIT_CRITICAL(&g_others_mux);
}

// ---- ビーコンの走査（BSCAN 命令・2026-10-14） ----
// 受信機が自分で ch1〜13 を順に回り、各チャンネルで聞こえたビーコンを BSSID ごとに数える。近くのルーターの
// チャンネル・方式（11b には CSI が無い）・RSSI を、ファームウェアの設定を変えずに調べるため
// （PC からは scripts/beacon_scan.py）。走査の間は番犬を止め、終わったら元のチャンネルに戻る
constexpr uint8_t FRAME_CTRL_BEACON = 0x80;     // 管理フレーム・Beacon
constexpr size_t FRAME_ADDR3 = 16;              // BSSID
constexpr size_t BEACON_INTERVAL_OFFSET = 32;   // ビーコンの間隔 [TU]（2 バイト）
constexpr size_t BEACON_IE_START = 36;          // 情報要素の始まり（時刻 8・間隔 2・能力 2 の後）
constexpr size_t FCS_LEN = 4;                   // sig_len に含まれる FCS [byte]
constexpr uint8_t IE_SSID = 0;
constexpr uint8_t IE_DS_PARAMS = 3;             // 送信するチャンネル（2.4 GHz）
constexpr uint8_t IE_HT_CAP = 45;               // 11n の能力（AP が 11n に対応）
constexpr size_t BEACON_SSID_MAX = 32;          // SSID の最大長 [byte]
constexpr size_t BEACON_SLOTS = 32;             // 1 チャンネルで数える BSSID の数
constexpr uint32_t BSCAN_DWELL_DEFAULT_MS = 1500;  // 1 チャンネルにとどまる時間 [ms]（ビーコン約 15 回）
constexpr uint32_t BSCAN_DWELL_MIN_MS = 200;
constexpr uint32_t BSCAN_DWELL_MAX_MS = 10000;

struct BeaconStat {
  uint8_t bssid[6];
  uint32_t n;            // ビーコンの数
  uint32_t n_csi;        // 同じ送信元から来た CSI の数（ビーコン以外のフレームも含む）
  int32_t rssi_sum;      // ビーコンの RSSI の合計 [dBm]
  uint16_t interval_tu;  // ビーコンの間隔 [TU = 1.024 ms]
  uint8_t ap_channel;    // ビーコンに書かれたチャンネル（0 = 書かれていない）
  uint8_t sig_mode;      // 最後のビーコンの方式（0 = 11b/g、1 = 11n）
  uint8_t rate;          // 最後のビーコンの速さの符号（rx_ctrl.rate。0〜7 = 11b、8〜15 = 11g）
  uint8_t ht_cap;        // ビーコンに 11n の能力の要素がある
  uint8_t ssid_len;      // 0 = 隠している
  uint8_t ssid[BEACON_SSID_MAX];
};
static BeaconStat g_beacons[BEACON_SLOTS];
static size_t g_beacons_used = 0;
static uint32_t g_beacons_overflow = 0;
static volatile bool g_bscan_active = false;  // 走査中だけ数える
static portMUX_TYPE g_beacons_mux = portMUX_INITIALIZER_UNLOCKED;

// 呼ぶ側で g_beacons_mux を取っておく。無ければ作る（あふれたら nullptr）。
static BeaconStat *beacon_slot(const uint8_t *bssid) {
  for (size_t i = 0; i < g_beacons_used; i++) {
    if (memcmp(g_beacons[i].bssid, bssid, 6) == 0) return &g_beacons[i];
  }
  if (g_beacons_used >= BEACON_SLOTS) return nullptr;
  BeaconStat *slot = &g_beacons[g_beacons_used++];
  memset(slot, 0, sizeof(*slot));
  memcpy(slot->bssid, bssid, 6);
  return slot;
}

static uint8_t g_channel_for_scan = 0;  // 走査で今いるチャンネル（違うチャンネルの名残りを数えない）

// プロミスキャスの管理フレームから呼ぶ（Wi-Fi タスク）。ビーコンでなければ何もしない。
static void record_beacon(const wifi_promiscuous_pkt_t *pkt) {
  if (!g_bscan_active) return;
  const uint8_t *f = pkt->payload;
  const size_t len = pkt->rx_ctrl.sig_len;
  if (len < BEACON_IE_START + FCS_LEN || f[FRAME_CTRL] != FRAME_CTRL_BEACON) return;
  if (pkt->rx_ctrl.channel != g_channel_for_scan) return;
  const size_t end = len - FCS_LEN;
  const uint8_t *ssid = nullptr;
  uint8_t ssid_len = 0;
  uint8_t ap_channel = 0;
  bool ht_cap = false;
  for (size_t i = BEACON_IE_START; i + 2 <= end;) {
    const uint8_t id = f[i];
    const uint8_t n = f[i + 1];
    if (i + 2 + n > end) break;  // 壊れた要素。そこまでで止める
    if (id == IE_SSID && n <= BEACON_SSID_MAX) {
      ssid = f + i + 2;
      ssid_len = n;
    } else if (id == IE_DS_PARAMS && n == 1) {
      ap_channel = f[i + 2];
    } else if (id == IE_HT_CAP) {
      ht_cap = true;
    }
    i += 2 + n;
  }
  uint16_t interval_tu = 0;
  memcpy(&interval_tu, f + BEACON_INTERVAL_OFFSET, sizeof(interval_tu));
  portENTER_CRITICAL(&g_beacons_mux);
  BeaconStat *slot = beacon_slot(f + FRAME_ADDR3);
  if (slot == nullptr) {
    g_beacons_overflow++;
  } else {
    slot->n++;
    slot->rssi_sum += pkt->rx_ctrl.rssi;
    slot->interval_tu = interval_tu;
    slot->ap_channel = ap_channel;
    slot->sig_mode = pkt->rx_ctrl.sig_mode;
    slot->rate = pkt->rx_ctrl.rate;
    slot->ht_cap = ht_cap ? 1 : 0;
    slot->ssid_len = ssid_len;
    if (ssid_len > 0) memcpy(slot->ssid, ssid, ssid_len);
  }
  portEXIT_CRITICAL(&g_beacons_mux);
}

// CSI のコールバックから呼ぶ（Wi-Fi タスク）。走査中に、ビーコンを数えている送信元の CSI を数える。
static void count_beacon_csi(const wifi_csi_info_t *info) {
  if (!g_bscan_active || info->rx_ctrl.channel != g_channel_for_scan) return;
  portENTER_CRITICAL(&g_beacons_mux);
  for (size_t i = 0; i < g_beacons_used; i++) {
    if (memcmp(g_beacons[i].bssid, info->mac, 6) == 0) {
      g_beacons[i].n_csi++;
      break;
    }
  }
  portEXIT_CRITICAL(&g_beacons_mux);
}
static volatile uint32_t g_dropped = 0;    // キュー満杯で捨てた数
static volatile uint32_t g_truncated = 0;  // CSI_MAX_LEN を超えて切り詰めた数
static volatile uint32_t g_written = 0;    // シリアルに書いた行数
static volatile uint32_t g_frames = 0;     // 主の送信機からのフレーム（プロミスキャス側）
static volatile uint32_t g_frames2 = 0;    // 2 台目の送信機からのデータフレーム
static volatile uint32_t g_parse_fail = 0; // 送信機からだが ESP-NOW/ペイロードとして読めなかった
static volatile uint32_t g_seq_unmatched = 0;  // 連番の対応が取れなかった CSI 行
static volatile int32_t g_seq_dt_max_us = 0;   // 対応づけた受信時刻の差の最大 [µs]
// 主の送信機の連番の飛び（液晶の欠け率に使う。プロミスキャス側で数える）
constexpr uint32_t SEQ_GAP_MAX = 1000;         // これより大きい飛びは送信機の再起動とみなして数えない
static volatile uint32_t g_seq_seen = 0;       // 連番を読めたフレームの数
static volatile uint32_t g_seq_missing = 0;    // 飛んだ連番の数
static volatile int8_t g_last_rssi_dbm = 0;    // 主の送信機の最後の CSI の RSSI [dBm]

// 今の受信チャンネル（loop だけが書く）
static uint8_t g_channel = WIFI_CHANNEL;
static uint32_t g_channel_switches = 0;  // apply_channel の回数（同じ値に戻っても切り替えを見分ける）

// 送信機への命令の返事（ESP-NOW 受信コールバックが書き、loop が読む）
static volatile uint32_t g_relay_token = 0;    // 待っている命令の番号
static volatile uint8_t g_relay_reply = 0;     // 届いた返事（CTRL_ACK / CTRL_NACK。0 はまだ）
static volatile uint32_t g_relay_ok = 0;       // 成功した切り替えの数
static volatile uint32_t g_relay_err = 0;      // 失敗した切り替えの数
static volatile uint32_t g_relay_nonce = 0;    // 待っている命令に使った合言葉

// 認証（setup で鍵を読む）
static uint8_t g_key[csi_auth::KEY_LEN] = {};
static bool g_auth = false;
static void setup_pair();  // 組を NVS から読む（定義は WHO の後）
static volatile uint32_t g_tx_nonce = 0;         // 送信機の今の合言葉（データのパケットから）
static volatile bool g_tx_nonce_known = false;
static volatile uint32_t g_ctrl_bad = 0;         // 署名・合言葉の合わない送信機の制御パケット
static volatile int8_t g_peer_qdbm = 0;          // 送信機の今の出力 [0.25 dBm]（データのパケットから）
static volatile bool g_peer_qdbm_known = false;
static volatile int8_t g_peer_temp_c = 0;         // 送信機のチップの温度 [℃]（データのパケットの版 4 から）
static volatile bool g_peer_temp_known = false;
static float g_temp_c = NAN;                      // 自分のチップの温度 [℃]（1 秒ごとに読む。内蔵センサーの目安）
static volatile uint8_t g_relay_value = 0;       // 了解に載った値（TXP_ACK の実際の出力）
static volatile uint32_t g_tx_restarts = 0;      // 送信機の連番が大きく戻った回数（送信機の再起動）

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

#if CSI_M5_LCD
// ---- スピーカー（M5Stack Basic の GPIO25・2026-10-11） -----------------------------
// ボタン C の印で短く鳴らす。押せたことがその場で分かり、スマホの動画の音声にも入るので、
// 画面に受信機が映らなくても動画と CSI を合わせる手がかりになる。鳴らし始めは印の行（# mark）を
// 書く直前（同じ loop の中）。止めるのは loop の speaker_update（待たない）
constexpr uint8_t SPEAKER_PIN = 25;
constexpr uint8_t SPEAKER_LEDC_CH = 0;          // 液晶のバックライトは 7 番（M5GFX）なので重ならない
constexpr uint32_t SPEAKER_DUTY = 128;          // ledcWriteTone は 10 ビット（50% = 512）。1/8 で音量を下げる
constexpr uint32_t BEEP_MARK_HZ = 2000;         // 印を記録した [Hz]
constexpr uint32_t BEEP_MARK_MS = 120;          // [ms]
constexpr uint32_t BEEP_NOT_REC_HZ = 500;       // 記録していないので印は残らない [Hz]（低く長い）
constexpr uint32_t BEEP_NOT_REC_MS = 400;       // [ms]

static bool g_beep_on = false;
static uint32_t g_beep_until_ms = 0;

static void speaker_init() {
  ledcAttachPin(SPEAKER_PIN, SPEAKER_LEDC_CH);
  ledcWriteTone(SPEAKER_LEDC_CH, 0);  // 鳴らさない（GPIO25 を浮かせると雑音が出る）
}

static void speaker_beep(uint32_t freq_hz, uint32_t dur_ms) {
  ledcWriteTone(SPEAKER_LEDC_CH, freq_hz);
  ledcWrite(SPEAKER_LEDC_CH, SPEAKER_DUTY);
  g_beep_on = true;
  g_beep_until_ms = millis() + dur_ms;
}

static void speaker_update() {
  if (!g_beep_on || static_cast<int32_t>(millis() - g_beep_until_ms) < 0) return;
  ledcWriteTone(SPEAKER_LEDC_CH, 0);
  g_beep_on = false;
}

// 印を付けたことを画面に出す時間 [ms]（状態の帯を「MARK n OK」に変える）
constexpr uint32_t MARK_FLASH_MS = 2000;
static uint32_t g_mark_flash_until_ms = 0;
static bool g_mark_flash_recorded = false;
static void mark_feedback(bool recorded);  // 液晶の節で定義する
#endif
#if CSI_SD
static uint32_t g_mark_count = 0;  // このセッションで記録した印の数（セッションを開くたびに 0）
#endif

// 受信機: 同期の合図（連番が LED_SYNC_EVERY の倍数のパケットを受けた。プロミスキャス受信が書く）
static volatile bool g_led_sync = false;
// 受信していないときに、ちらつかせる間隔 [ms] と、受信なしとみなす時間 [ms]
constexpr uint32_t LED_WAITING_EVERY_MS = 2000;
constexpr uint32_t LED_NO_SYNC_MS = 1500;

// ---- 失敗時の挙動: 黙って進まず、理由を出し続けて止まる ------------------------

// ---- 出力の振り分け: # 行はシリアルと SD（セッション中）の両方へ ----------------------
// SD のファイルを PC へ送る間は、シリアルへの CSI 行と # 行を止める（送っている中身と混ざらないように）
static volatile bool g_serial_paused = false;
constexpr size_t LOG_LINE_MAX = 640;  // log_printf の 1 行の最大 [byte]

static void note_write(const char *s, size_t n) {
  if (!g_serial_paused) Serial.write(reinterpret_cast<const uint8_t *>(s), n);
#if CSI_SD
  sd_log::append(s, n);
#endif
}

static void note_printf(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
static void note_printf(const char *fmt, ...) {
  char buf[LOG_LINE_MAX];
  va_list ap;
  va_start(ap, fmt);
  const int n = vsnprintf(buf, sizeof(buf), fmt, ap);
  va_end(ap);
  if (n <= 0) return;
  note_write(buf, static_cast<size_t>(n) < sizeof(buf) ? static_cast<size_t>(n) : sizeof(buf) - 1);
}

[[noreturn]] static void halt(const char *what, esp_err_t err) {
  for (;;) {
    Serial.printf("# FATAL: %s failed: %s (0x%x)\n", what, esp_err_to_name(err), err);
    led_fatal_blink(HALT_REPEAT_MS);
  }
}

static void check(esp_err_t err, const char *what) {
  if (err != ESP_OK) {
    halt(what, err);
  }
}

// ---- CRC32（IEEE 802.3。Python の zlib.crc32 と同じ値になる） ----------------

static uint32_t crc32_ieee(const uint8_t *data, size_t len) {
  constexpr uint32_t POLY_REFLECTED = 0xEDB88320;
  uint32_t crc = 0xFFFFFFFF;
  for (size_t i = 0; i < len; i++) {
    crc ^= data[i];
    for (int bit = 0; bit < 8; bit++) {
      crc = (crc >> 1) ^ (POLY_REFLECTED & (0u - (crc & 1u)));
    }
  }
  return ~crc;
}

// ---- プロミスキャス受信（Wi-Fi タスク）: 送信機の連番を受信時刻と一緒に控える -------

static void remember_seq(uint32_t rx_us, uint32_t seq, uint8_t tx) {
  portENTER_CRITICAL(&g_seq_mux);
  g_seq_ring[g_seq_count % SEQ_RING_SIZE] = {rx_us, seq, tx};
  g_seq_count++;
  portEXIT_CRITICAL(&g_seq_mux);
}

// 2 台目の送信機（Pico W）のデータフレーム: UDP の中の "CSIP" を探して連番を読む。
// 802.11 のヘッダー（QoS の有無で 24/26 バイト）+ LLC/SNAP + IPv4 + UDP の後にあるが、位置を決め打ちせず探す
static void on_tx2_data(const wifi_promiscuous_pkt_t *pkt) {
  const uint8_t *f = pkt->payload;
  const size_t len = pkt->rx_ctrl.sig_len;
  constexpr size_t kHeader = 24;       // 探し始める位置（802.11 のヘッダーの後）
  constexpr size_t kSearchEnd = 96;    // ここまでで見つからなければ別のパケット
  if (len < FRAME_ADDR2 + 6 || memcmp(f + FRAME_ADDR2, TX2_MAC, 6) != 0) return;
  // 数えるのは今のチャンネルで受けたものだけ。切り替えの直後にも前のチャンネルのフレームが
  // 処理されて届き、「新しいチャンネルで見えた」と取り違えた（2026-09-23）
  if (pkt->rx_ctrl.channel == g_channel) g_frames2++;
  const size_t end = len < kSearchEnd ? len : kSearchEnd;
  for (size_t i = kHeader; i + sizeof(CsiTxPayload) <= end; i++) {
    uint32_t magic = 0;
    memcpy(&magic, f + i, sizeof(magic));
    if (magic != PICO_MAGIC) continue;
    CsiTxPayload payload;
    memcpy(&payload, f + i, sizeof(payload));
    remember_seq(pkt->rx_ctrl.timestamp, payload.seq, 1);
    return;
  }
}

static void on_promiscuous(void *buf, wifi_promiscuous_pkt_type_t type) {
  if (buf == nullptr) return;
  if (type == WIFI_PKT_DATA) {
    on_tx2_data(static_cast<const wifi_promiscuous_pkt_t *>(buf));
    return;
  }
  if (type != WIFI_PKT_MGMT) return;
  const auto *pkt = static_cast<const wifi_promiscuous_pkt_t *>(buf);
  record_beacon(pkt);  // 走査中だけ（BSCAN）
  const uint8_t *f = pkt->payload;
  const size_t len = pkt->rx_ctrl.sig_len;
  if (len < FRAME_ADDR2 + sizeof(g_tx_mac)) return;
  if (memcmp(f + FRAME_ADDR2, g_tx_mac, sizeof(g_tx_mac)) != 0) return;
  if (pkt->rx_ctrl.channel == g_channel) g_frames++;  // 今のチャンネルの分だけ（g_frames2 と同じ）

  if (!g_first_frame_ready && len >= FIRST_FRAME_DUMP) {
    memcpy(g_first_frame, f, FIRST_FRAME_DUMP);
    g_first_frame_ready = true;
  }
  if (len < FIRST_FRAME_DUMP || f[FRAME_CTRL] != FRAME_CTRL_ACTION ||
      f[FRAME_CATEGORY] != CATEGORY_VENDOR || f[FRAME_ELEMENT_ID] != ELEMENT_VENDOR ||
      memcmp(f + FRAME_ELEMENT_OUI, ESPRESSIF_OUI, sizeof(ESPRESSIF_OUI)) != 0 ||
      f[FRAME_ELEMENT_TYPE] != ELEMENT_TYPE_ESPNOW) {
    g_parse_fail++;
    return;
  }
  CsiTxPayload payload;
  memcpy(&payload, f + FRAME_BODY, sizeof(payload));
  uint32_t magic = 0;
  memcpy(&magic, f + FRAME_BODY, sizeof(magic));
  if (magic == CTRL_MAGIC) return;  // 送信機の了解（制御パケット）。連番はない
  if (payload.magic != PAYLOAD_MAGIC) {
    g_parse_fail++;
    return;
  }
  if (payload.version >= PAYLOAD_VERSION_NONCE &&
      len >= static_cast<int>(FRAME_BODY + PAYLOAD_NONCE_OFFSET + sizeof(uint32_t))) {
    uint32_t nonce = 0;
    memcpy(&nonce, f + FRAME_BODY + PAYLOAD_NONCE_OFFSET, sizeof(nonce));
    g_tx_nonce = nonce;
    g_tx_nonce_known = true;
  }
  if (payload.version >= PAYLOAD_VERSION_TXP &&
      len >= static_cast<int>(FRAME_BODY + PAYLOAD_TXP_OFFSET + sizeof(int8_t))) {
    g_peer_qdbm = static_cast<int8_t>(f[FRAME_BODY + PAYLOAD_TXP_OFFSET]);
    g_peer_qdbm_known = true;
  }
  if (payload.version >= PAYLOAD_VERSION_TEMP &&
      len >= static_cast<int>(FRAME_BODY + PAYLOAD_TEMP_OFFSET + sizeof(int8_t))) {
    g_peer_temp_c = static_cast<int8_t>(f[FRAME_BODY + PAYLOAD_TEMP_OFFSET]);
    g_peer_temp_known = true;
  }
  remember_seq(pkt->rx_ctrl.timestamp, payload.seq, 0);
  {
    static uint32_t s_prev_seq = 0;
    static bool s_have_prev = false;
    const uint32_t gap = payload.seq - s_prev_seq;  // 重複（0）と巻き戻り・大きな飛びは数えない
    if (s_have_prev && gap >= 1 && gap <= SEQ_GAP_MAX) g_seq_missing += gap - 1;
    // 連番が大きく戻った = 送信機が再起動した（起動ごとに 0 から）
    if (s_have_prev && payload.seq < s_prev_seq && s_prev_seq - payload.seq > SEQ_GAP_MAX) {
      g_tx_restarts++;
    }
    s_prev_seq = payload.seq;
    s_have_prev = true;
    g_seq_seen++;
  }
  if (payload.seq % LED_SYNC_EVERY == 0) g_led_sync = true;  // 送信機が光るのと同じパケット
}

// 受信時刻が最も近い連番を探す。許容差を超えたら見つからない扱い。
static bool find_seq(uint32_t rx_us, uint8_t tx, uint32_t *seq, int32_t *dt_us) {
  bool found = false;
  int32_t best = SEQ_MATCH_TOLERANCE_US + 1;
  portENTER_CRITICAL(&g_seq_mux);
  const uint32_t n = g_seq_count < SEQ_RING_SIZE ? g_seq_count : SEQ_RING_SIZE;
  for (uint32_t i = 0; i < n; i++) {
    const SeqEntry &e = g_seq_ring[(g_seq_count - 1 - i) % SEQ_RING_SIZE];
    if (e.tx != tx) continue;
    const int32_t dt = static_cast<int32_t>(e.rx_us - rx_us);  // 32bit の一周をまたいでも正しい
    const int32_t adt = dt < 0 ? -dt : dt;
    if (adt < best) {
      best = adt;
      *seq = e.seq;
      *dt_us = dt;
      found = true;
    }
  }
  portEXIT_CRITICAL(&g_seq_mux);
  return found;
}

// ---- ESP-NOW 受信（Wi-Fi タスク）: 送信機の了解だけを控える ------------------------

static void on_espnow_recv(const uint8_t *mac, const uint8_t *data, int len) {
  if (mac == nullptr || memcmp(mac, g_tx_mac, sizeof(g_tx_mac)) != 0) return;
  if (data == nullptr || len < static_cast<int>(sizeof(uint32_t))) return;
  uint32_t magic = 0;
  memcpy(&magic, data, sizeof(magic));
  if (magic != CTRL_MAGIC) return;  // データのパケット（20 バイト）
  CsiCtrl ctrl;
  if (len != static_cast<int>(sizeof(ctrl))) {
    g_ctrl_bad++;
    return;
  }
  memcpy(&ctrl, data, sizeof(ctrl));
  if (ctrl.version != CTRL_VERSION || !g_auth || ctrl.nonce != g_relay_nonce ||
      !csi_auth::verify(g_key, reinterpret_cast<const uint8_t *>(&ctrl), CTRL_SIGNED_LEN,
                        ctrl.tag)) {
    g_ctrl_bad++;
    return;
  }
  if (ctrl.token == g_relay_token &&
      (ctrl.cmd == CTRL_ACK || ctrl.cmd == CTRL_NACK || ctrl.cmd == CTRL_TXP_ACK)) {
    g_relay_value = ctrl.channel;
    g_relay_reply = ctrl.cmd;
  }
}

// ---- CSI コールバック（Wi-Fi タスク。重い処理をしない） ------------------------

static void on_csi(void *ctx, wifi_csi_info_t *info) {
  (void)ctx;
  if (!info || !info->buf) return;
  const int tx = tx_index(info->mac);
  if (tx < 0) {
    g_other++;
    count_other(info);
    count_beacon_csi(info);  // 走査中だけ（BSCAN）
    if (!g_all_csi) return;
    g_all_csi_rows++;
  }
  if (tx == 0) {
    g_matched++;
    g_last_rssi_dbm = info->rx_ctrl.rssi;
  } else if (tx == 1) {
    g_matched2++;
  }

  CsiRecord rec;
  const wifi_pkt_rx_ctrl_t &rx = info->rx_ctrl;
  rec.rx_us = rx.timestamp;
  rec.rssi_dbm = rx.rssi;
  rec.noise_floor_dbm = rx.noise_floor;
  rec.channel = rx.channel;
  rec.secondary = rx.secondary_channel;
  rec.sig_mode = rx.sig_mode;
  rec.mcs = rx.mcs;
  rec.cwb = rx.cwb;
  rec.stbc = rx.stbc;
  rec.sgi = rx.sgi;
  rec.first_word_invalid = info->first_word_invalid ? 1 : 0;
  rec.len = info->len;
  rec.tx = tx < 0 ? TX_OTHER : static_cast<uint8_t>(tx);
  memcpy(rec.mac, info->mac, 6);
  if (rec.len > CSI_MAX_LEN) {
    rec.len = CSI_MAX_LEN;
    g_truncated++;
  }
  memcpy(rec.buf, info->buf, rec.len);

  if (xQueueSend(g_queue, &rec, 0) != pdTRUE) {
    g_dropped++;
  }
}

// ---- 書き出しタスク ---------------------------------------------------------

// 1 行の形式（FORMAT_VERSION = 3）:
//   CSI,<rx_us>,<tx>,<seq>,<rssi_dbm>,<noise_floor_dbm>,<channel>,<secondary>,<sig_mode>,<mcs>,<cwb>,
//       <stbc>,<sgi>,<first_word_invalid>,<ltf>,<len>,<hex>*<crc32>
// <tx> は送信元の MAC（12 桁の 16 進）。<seq> は送信機の連番（対応が取れなければ -1）。
// <ltf> は <hex> に入れた区間: htltf（11n。LLTF + HT-LTF のうち HT-LTF だけ）/ lltf（11g。LLTF だけ）/
// raw（その他の長さ。生データのまま）。<len> は <hex> のバイト数（htltf・lltf なら 128）。
// <hex> は CSI 生データ（int8 を 2 の補数で 2 桁の 16 進）。<crc32> は '*' より前の全体の CRC32。
static volatile long long g_last_seq_out = SEQ_UNKNOWN;  // 主の送信機の最後の CSI 行の連番
static volatile uint32_t g_last_rx_us = 0;                // 同じ行の受信時刻 [µs]

static void writer_task(void *arg) {
  (void)arg;
  static const char HEX_DIGITS[] = "0123456789abcdef";
  // 見出し最大 ~80 + 16 進 2 × CSI_MAX_LEN + CRC 等
  static char line[96 + 2 * CSI_MAX_LEN + 16];
  CsiRecord rec;
  for (;;) {
    if (xQueueReceive(g_queue, &rec, portMAX_DELAY) != pdTRUE) continue;

    if (g_first_frame_ready) {  // 診断: 最初の送信機フレームを 1 度だけ出す
      g_first_frame_ready = false;
      static bool s_dumped = false;
      if (!s_dumped) {
        s_dumped = true;
        char dump[2 * FIRST_FRAME_DUMP + 1];
        for (size_t i = 0; i < FIRST_FRAME_DUMP; i++) {
          snprintf(dump + 2 * i, 3, "%02x", g_first_frame[i]);
        }
        note_printf("# first_frame=%s\n", dump);
      }
    }

    // 連番を探す。プロミスキャス側が後から来ることもあるので、少し待ち直す
    // 他の送信元（ALLCSI）には連番が無いので探さない（待つと書き出しが遅れる）
    const bool other = rec.tx == TX_OTHER;
    uint32_t seq = 0;
    int32_t dt_us = 0;
    bool matched = !other && find_seq(rec.rx_us, rec.tx, &seq, &dt_us);
    for (uint32_t r = 0; !other && !matched && r < SEQ_WAIT_RETRIES; r++) {
      vTaskDelay(SEQ_WAIT_TICKS);
      matched = find_seq(rec.rx_us, rec.tx, &seq, &dt_us);
    }
    if (matched) {
      const int32_t adt = dt_us < 0 ? -dt_us : dt_us;
      if (adt > g_seq_dt_max_us) g_seq_dt_max_us = adt;
    } else if (!other) {
      g_seq_unmatched++;
    }
    const long long seq_out = matched ? static_cast<long long>(seq) : SEQ_UNKNOWN;

    // 出す区間: LLTF + HT-LTF（256 バイト以上）なら HT-LTF、LLTF だけ（128）ならそれ、他は生のまま
    const char *section = "raw";
    uint16_t off = 0;
    uint16_t n = rec.len;
    if (rec.len >= 2 * LTF_BYTES) {
      section = "htltf";
      off = LTF_BYTES;
      n = LTF_BYTES;
    } else if (rec.len == LTF_BYTES) {
      section = "lltf";
    }
    const uint8_t *m = other ? rec.mac : TX_LIST[rec.tx];
    int pos = snprintf(line, sizeof(line),
                       "CSI,%lu,%02x%02x%02x%02x%02x%02x,%lld,%d,%d,%u,%u,%u,%u,%u,%u,%u,%u,%s,%u,",
                       (unsigned long)rec.rx_us, m[0], m[1], m[2], m[3], m[4], m[5], seq_out,
                       rec.rssi_dbm, rec.noise_floor_dbm, rec.channel, rec.secondary,
                       rec.sig_mode, rec.mcs, rec.cwb, rec.stbc, rec.sgi,
                       rec.first_word_invalid, section, n);
    for (uint16_t i = off; i < off + n; i++) {
      const uint8_t b = static_cast<uint8_t>(rec.buf[i]);
      line[pos++] = HEX_DIGITS[b >> 4];
      line[pos++] = HEX_DIGITS[b & 0x0F];
    }
    const uint32_t crc = crc32_ieee(reinterpret_cast<const uint8_t *>(line), pos);
    pos += snprintf(line + pos, sizeof(line) - pos, "*%08lx\n", (unsigned long)crc);
    if (rec.tx == 0) {  // 主の送信機の最後の行（印の位置に使う）
      g_last_seq_out = seq_out;
      g_last_rx_us = rec.rx_us;
    }
    note_write(line, static_cast<size_t>(pos));
    g_written++;
  }
}

// ---- 初期化 ---------------------------------------------------------------

static void setup_radio() {
  WiFi.mode(WIFI_STA);   // Wi-Fi を起動する（どこにも接続しない）
  WiFi.disconnect();

  check(esp_wifi_set_ps(WIFI_PS_NONE), "esp_wifi_set_ps");  // 省電力で受信が間引かれるのを防ぐ
  check(esp_wifi_set_protocol(WIFI_IF_STA,
                              WIFI_PROTOCOL_11B | WIFI_PROTOCOL_11G | WIFI_PROTOCOL_11N),
        "esp_wifi_set_protocol");
  check(esp_wifi_set_bandwidth(WIFI_IF_STA, WIFI_BW_HT20), "esp_wifi_set_bandwidth");
  // 自分宛てでないブロードキャストも受けるため、プロミスキャスにしてからチャンネルを固定する
  check(esp_wifi_set_country_code(COUNTRY_CODE, false), "esp_wifi_set_country_code");
  check(esp_wifi_set_promiscuous(true), "esp_wifi_set_promiscuous");
  check(esp_wifi_set_channel(WIFI_CHANNEL, WIFI_SECOND_CHAN_NONE), "esp_wifi_set_channel");

  wifi_csi_config_t csi_config = {};
  csi_config.lltf_en = true;
  csi_config.htltf_en = true;
  csi_config.stbc_htltf2_en = true;
  csi_config.ltf_merge_en = false;       // LLTF と HT-LTF を平均せず、それぞれ残す
  csi_config.channel_filter_en = false;  // 隣接サブキャリアを平滑化しない（独立に残す）
  csi_config.manu_scale = false;         // 自動スケール（パケットごとに倍率が変わりうる。README 参照）
  csi_config.shift = 0;
  check(esp_wifi_set_csi_config(&csi_config), "esp_wifi_set_csi_config");
  check(esp_wifi_set_csi_rx_cb(on_csi, nullptr), "esp_wifi_set_csi_rx_cb");

  // 連番を読むため、管理フレーム（ESP-NOW は Action フレーム）をプロミスキャスで受ける
  wifi_promiscuous_filter_t filter = {};
  // 管理フレーム（ESP-NOW）に加えてデータフレームも受ける。データフレームを絞ると、その CSI も
  // 得られない（2026-09-23: Pico W の UDP ブロードキャストの CSI が 0 だった）。連番を読む処理は
  // on_promiscuous で管理フレームだけを見る
  filter.filter_mask = WIFI_PROMIS_FILTER_MASK_MGMT | WIFI_PROMIS_FILTER_MASK_DATA;
  check(esp_wifi_set_promiscuous_filter(&filter), "esp_wifi_set_promiscuous_filter");
  check(esp_wifi_set_promiscuous_rx_cb(on_promiscuous), "esp_wifi_set_promiscuous_rx_cb");

  // 送信機へ命令と合図を送るための ESP-NOW（宛先は送信機だけ。0 = 今のチャンネルで送る）
  check(esp_now_init(), "esp_now_init");
  check(esp_now_register_recv_cb(on_espnow_recv), "esp_now_register_recv_cb");
  // 組が未設定なら宛先を登録しない（全部 0 の MAC は登録できない。無線の命令も送らない）
  if (g_paired) {
    esp_now_peer_info_t peer = {};
    memcpy(peer.peer_addr, g_tx_mac, sizeof(g_tx_mac));
    peer.channel = 0;
    peer.ifidx = WIFI_IF_STA;
    peer.encrypt = false;  // 制御パケットは署名で守る（firmware/shared/CsiAuth）
    check(esp_now_add_peer(&peer), "esp_now_add_peer");
  }

  if (CSI_TX_POWER_QDBM > 0) {
    check(esp_wifi_set_max_tx_power(static_cast<int8_t>(CSI_TX_POWER_QDBM)),
          "esp_wifi_set_max_tx_power");
  }
}

// 今の送信出力の上限 [dBm]（読めなければ負の値）。
static float tx_power_dbm() {
  int8_t q = 0;
  if (esp_wifi_get_max_tx_power(&q) != ESP_OK) return -1.0f;
  return q / QDBM_PER_DBM;
}

// CSI を有効にするのは起動時の表示を出し終えてから。表示と CSI 行が混ざらないようにする。
// 以後の出力は 1 行 1 回の書き込み（printf / write）に限る。HardwareSerial は 1 回の
// 書き込みの中では排他されるが、println のような 2 回の書き込みの間には割り込まれる。
static void enable_csi() {
  check(esp_wifi_set_csi(true), "esp_wifi_set_csi");
}

// 起動時の表示（# 行の見出し）を out に書く。SD のセッションの先頭にも同じものを書く。
static void print_header(Print &out) {
  uint8_t primary = 0;
  wifi_second_chan_t second = WIFI_SECOND_CHAN_NONE;
  esp_wifi_get_channel(&primary, &second);

  uint8_t mac[6] = {};
  esp_wifi_get_mac(WIFI_IF_STA, mac);

  char country[3] = {};  // 2 文字 + 3 文字目（ESP-IDF は空白を入れる）。表示は先頭 2 文字だけ
  esp_wifi_get_country_code(country);

  // 先頭の改行: 直前の ROM 起動ログ（115200 bps で出て改行で終わらない）と
  // この行がつながり、取得側で読み捨てられるのを防ぐ
  out.printf("\n# CSI receiver format=%u\n", FORMAT_VERSION);
  out.printf("# mac=%02x%02x%02x%02x%02x%02x channel=%u (want %u) "
                "tx_mac=%02x%02x%02x%02x%02x%02x baud=%lu country=%.2s "
                "commands=CH,CHLOCAL,WHO,OTHERS,BSCAN,ALLCSI,PJOIN,PLEAVE,TXP,TXPS,TXREBOOT,WDTTEST,REBOOT,"
                "PAIRSET,PAIRCLEAR relay=1 pico_relay=%d led_pin=%u "
                "auth=%s paired=%u tx_power_dbm=%.2f fw=%s boot=%s\n",
                mac[0], mac[1], mac[2], mac[3], mac[4], mac[5], primary, WIFI_CHANNEL,
                g_tx_mac[0], g_tx_mac[1], g_tx_mac[2], g_tx_mac[3], g_tx_mac[4], g_tx_mac[5],
                (unsigned long)SERIAL_BAUD, country, PICO_RELAY ? 1 : 0, LED_PIN,
                g_auth ? "on" : "off_no_key", g_paired ? 1u : 0u, tx_power_dbm(), CSI_FW_VERSION,
                csi_boot::reset_reason_name());
  if (!g_paired) out.print("# not_paired (set with PAIRSET / csikit flash)\n");
  for (size_t i = 0; i < N_TX; i++) {
    const uint8_t *t = TX_LIST[i];
    out.printf("# tx id=%u mac=%02x%02x%02x%02x%02x%02x name=%s%s\n", (unsigned)i, t[0], t[1],
                  t[2], t[3], t[4], t[5], TX_NAMES[i], i == 0 ? " primary=1" : "");
  }
  out.print("# columns=CSI,rx_us,tx,seq,rssi_dbm,noise_floor_dbm,channel,secondary,sig_mode,"
            "mcs,cwb,stbc,sgi,first_word_invalid,ltf,len,hex*crc32\n");
}

// 自己診断: 設定したつもりの値と、実際に効いている値を並べて出す。
static void report_config() {
  uint8_t primary = 0;
  wifi_second_chan_t second = WIFI_SECOND_CHAN_NONE;
  check(esp_wifi_get_channel(&primary, &second), "esp_wifi_get_channel");
  uint8_t mac[6] = {};
  check(esp_wifi_get_mac(WIFI_IF_STA, mac), "esp_wifi_get_mac");
  print_header(Serial);
  if (primary != WIFI_CHANNEL) {
    halt("channel verification", ESP_ERR_INVALID_STATE);
  }
}

#if CSI_SD
static void sd_setup();
static void ntp_sync();
#endif

void setup() {
  led_init();
  Serial.setTxBufferSize(SERIAL_TX_BUFFER);  // begin より前に呼ぶ必要がある
  Serial.begin(SERIAL_BAUD);
  delay(1000);

  g_queue = xQueueCreate(QUEUE_DEPTH, sizeof(CsiRecord));
  if (g_queue == nullptr) {
    halt("xQueueCreate", ESP_ERR_NO_MEM);
  }
  if (xTaskCreate(writer_task, "csi_writer", WRITER_STACK, nullptr, WRITER_PRIORITY,
                  nullptr) != pdPASS) {
    halt("xTaskCreate", ESP_ERR_NO_MEM);
  }

#if CSI_SD
  Serial.write('\n');  // 改行で終わらない ROM の起動ログ（115200 bps）と # ntp の行をつなげない
  ntp_sync();  // 受信を始める前に（手元の Wi-Fi に一度つないで切る。チャンネルは setup_radio で固定する）
#endif
  setup_pair();  // setup_radio が送信機を宛先に登録するので、その前に
  setup_radio();
  report_config();
#if CSI_M5_LCD
  csi_lcd_begin(CSI_LABEL, "RECEIVER", "fw " CSI_FW_VERSION);  // 失敗時の理由は '#' 行（取得側では注記）
  speaker_init();
#endif
#if CSI_SD
  sd_setup();  // 液晶の後に（液晶が SPI バスを初期化してから、SD も同じバスを使う）
#endif
  note_printf("# receiving\n");
  Serial.flush();
  enable_csi();
  csi_boot::start_loop_watchdog();  // 以後 loop が 5 秒止まると再起動する
}

// ---- シリアルの命令 ---------------------------------------------------------
// 応答は 1 行 1 回の printf で書く（書き出しタスクの CSI 行と混ざらないように）。

// チャンネルを切り替えて読み戻す。成功なら g_channel を更新して nullptr、失敗なら理由を返す。
static const char *apply_channel(uint8_t ch) {
  const esp_err_t err = esp_wifi_set_channel(ch, WIFI_SECOND_CHAN_NONE);
  if (err != ESP_OK) return esp_err_to_name(err);
  uint8_t primary = 0;
  wifi_second_chan_t second = WIFI_SECOND_CHAN_NONE;
  g_channel_switches++;  // 失敗しても、前のチャンネルで数えた分は使えない
  if (esp_wifi_get_channel(&primary, &second) != ESP_OK || primary != ch) return "verify_failed";
  g_channel = primary;
  return nullptr;
}

// 命令の引数（1〜13）を読む。範囲外なら err を出して 0 を返す。
static uint8_t parse_channel(const char *cmd, const char *arg) {
  char *end = nullptr;
  const long ch = strtol(arg, &end, 10);
  if (end == arg || *end != '\0' || ch < CHANNEL_MIN || ch > CHANNEL_MAX) {
    note_printf("# err cmd=%s reason=out_of_range arg=%s min=%u max=%u\n", cmd, arg,
                  CHANNEL_MIN, CHANNEL_MAX);
    return 0;
  }
  return static_cast<uint8_t>(ch);
}

// 送信機のパケットが ms の間に min_frames 以上届くか（今のチャンネルで）。
static bool link_alive(uint32_t ms, uint32_t min_frames) {
  const uint32_t start = g_frames;
  const uint32_t t0 = millis();
  while (millis() - t0 < ms) {
    if (g_frames - start >= min_frames) return true;
    csi_boot::wait_ms(1);
  }
  return false;
}

// 送信機に署名した制御パケットを送る。鍵がなければ送らない。
static void send_ctrl(uint8_t cmd, uint8_t channel, uint32_t token, uint32_t nonce) {
  if (!g_auth) return;
  CsiCtrl ctrl = {CTRL_MAGIC, CTRL_VERSION, cmd, channel, token, nonce, {}};
  if (!csi_auth::sign(g_key, reinterpret_cast<const uint8_t *>(&ctrl), CTRL_SIGNED_LEN,
                      ctrl.tag)) {
    return;
  }
  esp_now_send(g_tx_mac, reinterpret_cast<const uint8_t *>(&ctrl), sizeof(ctrl));
}

// ---- 2 台目の送信機（Pico W）: AP に STA として接続し、UDP で命令と合図を送る ------------------
// Pico W は ESP-NOW を受けられない。Pico W の AP に接続している間は自分のチャンネルを変えられないので、
// 切り替えの前に切断し、切り替えた後に接続し直す。

static WiFiUDP g_pico_udp;
static bool g_pico_udp_ready = false;
static bool g_pico_joined = false;     // 接続している（と思っている）。切れたら loop で気づく
// Pico W の居場所の記録（接続していない時に「寄り道」して命令を届けるため。2026-09-24）
static uint8_t g_pico_channel = 0;       // 最後にいたと分かっているチャンネル（0 = 不明）
static uint32_t g_pico_contact_ms = 0;   // 最後に Pico W と話せた時刻（接続・合図）
static uint16_t g_pico_disallowed = 0;   // Pico W が not_allowed を返したチャンネル（1 << ch）
static uint32_t g_pico_join_ok = 0;    // 接続できた回数
static uint32_t g_pico_join_err = 0;   // 接続できなかった回数

static bool pico_connected() {
  return g_pico_joined && g_pico_udp_ready && WiFi.status() == WL_CONNECTED;
}

// 今のチャンネルの Pico W の AP に接続する。成功なら nullptr、失敗なら理由。
// 接続のあいだだけ、国の設定で使えるチャンネルを今のチャンネル 1 つに絞る。絞らないと、Pico W が
// チャンネルを変えた後の接続で、指定したチャンネルを使わず ch1 から順に全チャンネルを探し、その間
// CSI が止まった（ch6 で 0.8 秒・ch11 で 1.25 秒。AP の記録を消しても同じ。2026-09-23 実測）。
// 絞ると、Pico W がいなければ他のチャンネルへ行かずに失敗する（S のチャンネルを離れない）。
static const char *pico_join(uint32_t *ms_out) {
  if (!PICO_RELAY) return "disabled";  // 自動接続・寄り道・PJOIN のすべてがここを通る
  const uint32_t t0 = millis();
  WiFi.setAutoReconnect(false);  // 見失っても勝手に全チャンネルを探しに行かない
  if (!WiFi.config(PICO_STA_IP, PICO_AP_IP, PICO_NETMASK)) return "config";
  wifi_country_t one = {};
  memcpy(one.cc, COUNTRY_CODE, 2);
  one.schan = g_channel;
  one.nchan = 1;
  one.policy = WIFI_COUNTRY_POLICY_MANUAL;
  const esp_err_t narrowed = esp_wifi_set_country(&one);
  WiFi.begin(PICO_SSID, nullptr, g_channel, TX2_MAC, true);
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < PICO_JOIN_TIMEOUT_MS) csi_boot::wait_ms(1);
  const bool connected = WiFi.status() == WL_CONNECTED;
  if (!connected) WiFi.disconnect(false, false);  // 探し続けさせない
  wifi_country_t all = one;
  all.schan = CHANNEL_MIN;
  all.nchan = CHANNEL_MAX - CHANNEL_MIN + 1;
  const esp_err_t restored = esp_wifi_set_country(&all);
  esp_wifi_set_ps(WIFI_PS_NONE);  // 接続で省電力に戻らないように（受信が間引かれる）
  *ms_out = millis() - t0;
  if (narrowed != ESP_OK || restored != ESP_OK) return "country";
  uint8_t primary = 0;
  wifi_second_chan_t second = WIFI_SECOND_CHAN_NONE;
  esp_wifi_get_channel(&primary, &second);
  if (!connected) {
    g_pico_join_err++;
    return "timeout";
  }
  if (primary != g_channel) {  // 絞っているので起きないはずだが、S のチャンネルを離れたら戻る
    WiFi.disconnect(false, false);
    apply_channel(g_channel);
    g_pico_join_err++;
    return "wrong_channel";
  }
  if (!g_pico_udp_ready) g_pico_udp_ready = g_pico_udp.begin(PICO_REPLY_PORT);
  if (!g_pico_udp_ready) return "udp";
  g_pico_joined = true;
  g_pico_channel = g_channel;
  g_pico_contact_ms = millis();
  g_pico_join_ok++;
  return nullptr;
}

// 切断する（チャンネルを変える前に必ず呼ぶ）。
static void pico_leave() {
  const uint32_t t0 = millis();
  WiFi.disconnect(false, false);
  while (WiFi.status() == WL_CONNECTED && millis() - t0 < PICO_JOIN_TIMEOUT_MS) csi_boot::wait_ms(1);
  g_pico_joined = false;
}

static bool pico_send(const char *msg) {
  const size_t len = strlen(msg);
  return g_pico_udp.beginPacket(PICO_AP_IP, PICO_CMD_PORT) &&
         g_pico_udp.write(reinterpret_cast<const uint8_t *>(msg), len) == len &&
         g_pico_udp.endPacket();
}

// Pico W に "CH <n>" を送り、同じチャンネルの ack を待つ（PICO_ATTEMPTS 回まで送り直す）。
// 成功なら nullptr（Pico W は ack の CMD_SWITCH_DELAY_MS 後に切り替える）、失敗なら理由。
static const char *pico_request_switch(uint8_t ch) {
  if (!pico_connected()) return "not_joined";
  while (g_pico_udp.parsePacket() > 0) g_pico_udp.flush();  // 前の返事の残りを捨てる
  char msg[16];
  snprintf(msg, sizeof(msg), "CH %u\n", ch);
  char want[32];
  snprintf(want, sizeof(want), "ack cmd=CH channel=%u ", ch);
  for (uint32_t attempt = 0; attempt < PICO_ATTEMPTS; attempt++) {
    pico_send(msg);
    const uint32_t t0 = millis();
    while (millis() - t0 < PICO_REPLY_TIMEOUT_MS) {
      if (g_pico_udp.parsePacket() > 0) {
        char buf[97] = {};
        const int len = g_pico_udp.read(buf, sizeof(buf) - 1);
        if (len > 0 && strncmp(buf, want, strlen(want)) == 0) return nullptr;
        if (len > 0 && strncmp(buf, "err", 3) == 0) {
          // Pico W が使えないチャンネル（チップの国の設定で ch1〜11 だけ。2026-09-24）
          if (strstr(buf, "reason=not_allowed") == nullptr) return "pico_err";
          g_pico_disallowed |= static_cast<uint16_t>(1u << ch);  // 次からは寄り道しない
          return "not_allowed";
        }
      }
      csi_boot::wait_ms(1);
    }
  }
  return "no_ack";
}

// 2 台目の送信機のフレームが ms の間に min_frames 以上届くか（今のチャンネルで）。
static bool link2_alive(uint32_t ms, uint32_t min_frames) {
  const uint32_t start = g_frames2;
  const uint32_t t0 = millis();
  while (millis() - t0 < ms) {
    if (g_frames2 - start >= min_frames) return true;
    csi_boot::wait_ms(1);
  }
  return false;
}

// 切り替えた後: Pico W が新しいチャンネルに来たか確かめて接続し直す。結果（ok / no_link / join_*）。
static const char *pico_follow_up() {
  if (!link2_alive(LINK_CHECK_MS, LINK_MIN_FRAMES)) return "no_link";
  uint32_t ms = 0;
  const char *reason = pico_join(&ms);
  return reason == nullptr ? "ok" : reason;
}

// Pico W だけを from から to へ移す（S の切り替えが失敗して、先に動いた Pico W を呼び戻す時）。
// 自分は一時的に from へ行き、終わったら to に戻る。結果（ok / 理由）。
static const char *pico_move(uint8_t from, uint8_t to) {
  if (apply_channel(from) != nullptr) return "apply_from";
  if (!link2_alive(LINK_CHECK_MS, LINK_MIN_FRAMES)) {
    apply_channel(to);
    return "not_found";
  }
  uint32_t ms = 0;
  const char *reason = pico_join(&ms);
  if (reason == nullptr) {
    reason = pico_request_switch(to);
    pico_leave();
  }
  apply_channel(to);
  if (reason != nullptr) return reason;
  return pico_follow_up();
}

// PJOIN: 今のチャンネルの Pico W に接続する（手で確かめる用。普段は loop が自動で接続する）。
static void command_pico_join() {
  if (pico_connected()) pico_leave();
  uint32_t ms = 0;
  const char *reason = pico_join(&ms);
  if (reason != nullptr) {
    note_printf("# err cmd=PJOIN reason=%s ms=%lu channel=%u\n", reason, (unsigned long)ms,
                  g_channel);
    return;
  }
  note_printf("# ack cmd=PJOIN ms=%lu channel=%u ip=%s\n", (unsigned long)ms, g_channel,
                WiFi.localIP().toString().c_str());
}

// PLEAVE: 切断する（手で確かめる用。切断している間は loop が自動で接続し直す）。
static void command_pico_leave() {
  pico_leave();
  note_printf("# ack cmd=PLEAVE channel=%u\n", g_channel);
}

// Pico W との接続の見張り: 合図（KA）を送り、切れたら気づき、見えていれば接続し直す（loop から）。
static void service_pico() {
  if (!PICO_RELAY) return;
  static uint32_t s_ka_ms = 0;
  static uint32_t s_try_ms = 0;
  static uint32_t s_frames2 = 0;
  static uint32_t s_seen_ms = 0;
  static uint32_t s_since_switch = 0;  // 今のチャンネルに来てから見た Pico W のフレーム数
  static uint32_t s_seen_switches = UINT32_MAX;
  const uint32_t now = millis();
  // チャンネルの値でなく切り替えの回数で見る（6 へ行って 11 に戻る命令が続けて来ると、値は同じまま
  // 前の 11 で数えた分が残り、いない Pico W に接続を試した。2026-09-23）
  if (g_channel_switches != s_seen_switches) {
    // 前のチャンネルで見た分は数えない。切り替え直後に届く数フレームも数えないよう、来てから
    // LINK_MIN_FRAMES 以上見て初めて「見えている」とする（数えると、いない Pico W に接続を試して
    // 1 秒待たされた。2026-09-23）
    s_seen_switches = g_channel_switches;
    s_since_switch = 0;
    s_frames2 = g_frames2;
  }
  if (g_frames2 != s_frames2) {
    s_since_switch += g_frames2 - s_frames2;
    s_frames2 = g_frames2;
    s_seen_ms = now;
  }
  const bool s_seen = s_since_switch >= LINK_MIN_FRAMES;
  if (g_pico_joined && WiFi.status() != WL_CONNECTED) {
    g_pico_joined = false;
    note_printf("# pico lost channel=%u\n", g_channel);
  }
  if (pico_connected()) {
    if (now - s_ka_ms >= PICO_KEEPALIVE_MS) {
      s_ka_ms = now;
      if (pico_send("KA")) g_pico_contact_ms = now;
    }
    return;
  }
  // 今のチャンネルで Pico W が見えている時だけ接続を試す（いなければ試さない。試す間は命令を待たせる）
  if (!s_seen || now - s_seen_ms > PICO_VISIBLE_MS || now - s_try_ms < PICO_REJOIN_MS) return;
  s_try_ms = now;
  uint32_t ms = 0;
  const char *reason = pico_join(&ms);
  if (reason == nullptr) {
    note_printf("# pico joined ms=%lu channel=%u\n", (unsigned long)ms, g_channel);
  } else {
    note_printf("# pico join_failed reason=%s ms=%lu channel=%u\n", reason, (unsigned long)ms,
                  g_channel);
  }
}

// CHLOCAL: 自分だけ切り替える（送信機はシリアルで別に切り替える使い方）。
static void command_channel_local(const char *arg) {
  const uint8_t ch = parse_channel("CHLOCAL", arg);
  if (ch == 0) return;
  if (pico_connected()) pico_leave();  // 接続したままではチャンネルを変えられない
  const char *reason = apply_channel(ch);
  if (reason != nullptr) {
    note_printf("# err cmd=CHLOCAL reason=%s channel=%u\n", reason, ch);
    return;
  }
  led_start(LED_SWITCH, LED_EVENT);
  note_printf("# ack cmd=CHLOCAL channel=%u\n", g_channel);
}

// S に無線で伝えてから、S と自分を切り替える。成功なら nullptr、失敗なら理由（err の行は呼び出し側）。
// 失敗のとき自分は元のチャンネル（見つからなければ起動時のチャンネル）に戻っている（g_channel）。
static const char *relay_channel(uint8_t ch, uint32_t *attempts_out, bool *acked_out) {
  static uint32_t s_token = 0;
  const uint8_t prev = g_channel;
  *attempts_out = 0;
  *acked_out = false;
  if (!g_auth) return "no_key";
  if (!g_tx_nonce_known) return "no_nonce";
  const uint32_t token = ++s_token;
  const uint32_t nonce = g_tx_nonce;  // 同じ命令の送り直しは同じ合言葉で（受け入れられたら送信機が変える）
  g_relay_reply = 0;
  g_relay_nonce = nonce;
  g_relay_token = token;
  uint32_t attempts = 0;
  while (attempts < RELAY_ATTEMPTS && g_relay_reply == 0) {
    attempts++;
    send_ctrl(CTRL_SET_CH, ch, token, nonce);
    const uint32_t t0 = millis();
    while (g_relay_reply == 0 && millis() - t0 < RELAY_ACK_TIMEOUT_MS) csi_boot::wait_ms(1);
  }
  *attempts_out = attempts;
  const uint8_t reply = g_relay_reply;
  *acked_out = reply == CTRL_ACK;
  if (reply == CTRL_NACK) return "tx_nack";
  if (reply == 0 && link_alive(LINK_CHECK_MS, LINK_MIN_FRAMES)) {
    // 了解が来ず、送信機はまだ今のチャンネルにいる → 命令が届いていない
    return "tx_no_ack";
  }
  // 了解が来た、または了解だけ失われて送信機はもう移った（今のチャンネルから消えた）
  const char *reason = apply_channel(ch);
  if (reason == nullptr && link_alive(LINK_CHECK_MS, LINK_MIN_FRAMES)) return nullptr;
  // 新しいチャンネルで送信機が見つからない。元のチャンネルに戻って探し、だめなら起動時のチャンネルへ
  apply_channel(prev);
  if (link_alive(LINK_CHECK_MS, LINK_MIN_FRAMES)) return "no_link";
  apply_channel(WIFI_CHANNEL);
  // 送信機も合図が途絶えれば起動時のチャンネルに戻るので、両方がそこで出会う
  return "lost_link";
}

// 接続していない Pico W がいるはずのチャンネル（0 = 分からない）。最後に話してから PICO_WATCHDOG_GUESS_MS
// たっていれば、Pico W は番犬で起動時のチャンネルに戻っているとみなす。
static uint8_t pico_expected_channel() {
  if (g_pico_channel == 0) return 0;
  if (millis() - g_pico_contact_ms < PICO_WATCHDOG_GUESS_MS) return g_pico_channel;
  return WIFI_CHANNEL;
}

// 寄り道: 接続していない Pico W のいるはずのチャンネルへ行き、接続して ch へ動くよう伝え、自分は back に
// 戻る。いなければもう一方の候補（起動時のチャンネル / 最後にいたチャンネル）を試す。
// 伝えられたら nullptr（*found_at に見つけたチャンネル）、だめなら理由（not_allowed / not_found）。
static const char *pico_detour(uint8_t ch, uint8_t back, uint8_t *found_at) {
  const uint8_t first = pico_expected_channel();
  const uint8_t second = first == WIFI_CHANNEL ? g_pico_channel : WIFI_CHANNEL;
  const uint8_t cands[2] = {first, second};
  const char *result = "not_found";
  for (size_t i = 0; i < 2; i++) {
    const uint8_t c = cands[i];
    if (c == 0 || (i == 1 && c == first)) continue;
    if (c != g_channel && apply_channel(c) != nullptr) continue;
    if (!link2_alive(LINK_CHECK_MS, LINK_MIN_FRAMES)) continue;
    uint32_t ms = 0;
    if (pico_join(&ms) != nullptr) continue;
    const char *reason = pico_request_switch(ch);
    pico_leave();
    if (reason == nullptr) {
      *found_at = c;
      result = nullptr;
      break;
    }
    result = reason;  // not_allowed など（Pico W は c に残る）
    break;
  }
  if (g_channel != back) apply_channel(back);
  return result;
}

// CH: S と Pico W に無線で伝えてから、3 台を切り替える。
// 順番: Pico W に UDP で伝える（ack の 100 ms 後に Pico W が動く）→ 切断 → S に ESP-NOW で伝えて
// S と自分を切り替える → Pico W のフレームを確かめて接続し直す。Pico W に届かなくても S は切り替え、
// 結果を pico= に出す（ok / not_joined / not_found / not_allowed / no_ack / pico_err / no_link / join_* /
// recalled_*）。接続していなくても居場所が分かっていれば寄り道して伝える（ack に detour=<寄った先>）。
// Pico W が使えないチャンネル（ch12・13）へ行くと Pico W は残り、接続も切れるので、次の切り替えで寄り道する。
static void command_channel(const char *arg) {
  const uint8_t ch = parse_channel("CH", arg);
  if (ch == 0) return;
  const uint8_t prev = g_channel;
  const char *pico = "not_joined";
  bool pico_moving = false;
  uint8_t detour = 0;
  const bool disallowed = (g_pico_disallowed & (1u << ch)) != 0;
  if (pico_connected() && disallowed) {
    pico = "not_allowed";  // 伝えても動けない。Pico W は今のチャンネルに残る
    pico_leave();
  } else if (pico_connected()) {
    const char *reason = pico_request_switch(ch);
    pico_moving = reason == nullptr;
    pico = pico_moving ? "moving" : reason;
    pico_leave();  // 接続したままではチャンネルを変えられない
  } else if (disallowed) {
    pico = "not_allowed";
  } else if (pico_expected_channel() == ch) {
    pico_moving = true;  // Pico W はもう ch にいるはず（切り替え後に確かめて接続する）
  } else if (pico_expected_channel() != 0) {
    const char *reason = pico_detour(ch, prev, &detour);
    pico_moving = reason == nullptr;
    pico = pico_moving ? "moving" : reason;
  }
  uint32_t attempts = 0;
  bool acked = false;
  const char *reason = relay_channel(ch, &attempts, &acked);
  if (reason == nullptr) {
    g_relay_ok++;
    if (pico_moving) pico = pico_follow_up();
    led_start(LED_SWITCH, LED_EVENT);
    note_printf("# ack cmd=CH channel=%u relay=ok acked=%u attempts=%lu pico=%s detour=%u\n",
                  g_channel, acked ? 1u : 0u, (unsigned long)attempts, pico, detour);
    return;
  }
  g_relay_err++;
  if (strcmp(reason, "tx_nack") != 0 && strcmp(reason, "tx_no_ack") != 0) {
    led_start(LED_ALERT, LED_ERROR);
  }
  if (pico_moving && g_channel != ch) {
    // S は来なかったが Pico W は ch に動いた。Pico W を今のチャンネルに呼び戻す
    const char *back = pico_move(ch, g_channel);
    pico = strcmp(back, "ok") == 0 ? "recalled" : "recall_failed";
  } else if (pico_moving) {
    pico = pico_follow_up();
  } else if (strcmp(pico, "not_joined") != 0 && g_channel == prev) {
    // Pico W は動いていない（届かなかった）。元のチャンネルのまま接続し直す
    uint32_t ms = 0;
    pico_join(&ms);
  }
  if (strcmp(reason, "tx_nack") == 0 || strcmp(reason, "tx_no_ack") == 0) {
    note_printf("# err cmd=CH reason=%s channel=%u attempts=%lu pico=%s\n", reason, ch,
                  (unsigned long)attempts, pico);
  } else {
    note_printf("# err cmd=CH reason=%s channel=%u back_to=%u pico=%s\n", reason, ch, g_channel,
                  pico);
  }
}

// 送信機へ合図を送る（KEEPALIVE_MS ごと）。
static void send_keepalive() {
  static uint32_t s_last_ms = 0;
  const uint32_t now = millis();
  if (now - s_last_ms < KEEPALIVE_MS) return;
  s_last_ms = now;
  if (g_tx_nonce_known) send_ctrl(CTRL_KEEPALIVE, g_channel, 0, g_tx_nonce);
}

// 番犬: 起動時のチャンネル以外で送信機のパケットが途絶えたら、起動時のチャンネルに戻る。
// Pico W は合図（KA）が途絶えると自分の番犬で起動時のチャンネルに戻るので、そこで接続し直す。
static void check_watchdog() {
  static uint32_t s_frames = 0;
  static uint32_t s_since_ms = 0;
  const uint32_t now = millis();
  if (g_frames != s_frames || g_channel == WIFI_CHANNEL || g_all_csi) {  // ALLCSI の間は止める
    s_frames = g_frames;
    s_since_ms = now;
    return;
  }
  if (now - s_since_ms < WATCHDOG_MS) return;
  const uint8_t from = g_channel;
  if (pico_connected()) pico_leave();  // 接続したままではチャンネルを変えられない
  const char *reason = apply_channel(WIFI_CHANNEL);
  led_start(LED_ALERT, LED_ERROR);
  note_printf("# watchdog no_frames_ms=%lu from=%u back_to=%u result=%s\n",
                (unsigned long)(now - s_since_ms), from, WIFI_CHANNEL,
                reason == nullptr ? "ok" : reason);
  s_since_ms = now;
}

// OTHERS: 送信機以外の CSI を送信元ごとに 1 行ずつ出してリセットする。
static void command_others() {
  OtherStat copy[OTHER_SLOTS];
  size_t used = 0;
  uint32_t overflow = 0;
  portENTER_CRITICAL(&g_others_mux);
  used = g_others_used;
  overflow = g_others_overflow;
  memcpy(copy, g_others, sizeof(OtherStat) * used);
  g_others_used = 0;
  g_others_overflow = 0;
  portEXIT_CRITICAL(&g_others_mux);
  for (size_t i = 0; i < used; i++) {
    const OtherStat &o = copy[i];
    note_printf("# other mac=%02x%02x%02x%02x%02x%02x n=%lu ht=%lu last_rate=%u last_mcs=%u "
                  "last_len=%u rssi_mean_dbm=%ld\n", o.mac[0], o.mac[1], o.mac[2], o.mac[3],
                  o.mac[4], o.mac[5], (unsigned long)o.n, (unsigned long)o.n_ht, o.last_rate,
                  o.last_mcs, o.last_len, o.n ? (long)(o.rssi_sum / (int32_t)o.n) : 0L);
  }
  note_printf("# others sources=%u overflow=%lu\n", (unsigned)used, (unsigned long)overflow);
}

// BSCAN [dwell_ms]: ch1〜13 を順に回り、各チャンネルで聞こえたビーコンを BSSID ごとに 1 行ずつ出す。
// 1 行: # beacon ch=<今いるチャンネル> bssid=<12 桁> ap_ch=<ビーコンに書かれたチャンネル> n=<ビーコンの数>
//   csi=<同じ送信元の CSI の数> sig_mode=<0/1> rate=<速さの符号> ht_cap=<0/1> interval_tu=<間隔>
//   rssi_mean_dbm=<平均> ssid_hex=<SSID の 16 進（空 = 隠している）>
// チャンネルごとに # bscan ch=<n> dwell_ms=<ms> sources=<数> overflow=<数>（切り替え失敗は err=<理由>）、
// 最後に # bscan done back_to=<戻ったチャンネル> result=<ok/理由>。止まっている間（約 dwell × 13）は CSI を出さない。
// SD に記録している間は断る（記録に穴が空くため）。
static void command_bscan(const char *arg) {
  uint32_t dwell_ms = BSCAN_DWELL_DEFAULT_MS;
  if (arg != nullptr) {
    char *end = nullptr;
    const unsigned long v = strtoul(arg, &end, 10);
    if (end == arg || *end != '\0' || v < BSCAN_DWELL_MIN_MS || v > BSCAN_DWELL_MAX_MS) {
      note_printf("# err cmd=BSCAN reason=out_of_range arg=%s min=%lu max=%lu\n", arg,
                    (unsigned long)BSCAN_DWELL_MIN_MS, (unsigned long)BSCAN_DWELL_MAX_MS);
      return;
    }
    dwell_ms = static_cast<uint32_t>(v);
  }
#if CSI_SD
  if (sd_log::recording()) {
    note_printf("# err cmd=BSCAN reason=recording\n");
    return;
  }
#endif
  if (pico_connected()) pico_leave();  // 接続したままではチャンネルを変えられない
  const uint8_t back = g_channel;
  note_printf("# bscan start dwell_ms=%lu channels=%u-%u back_to=%u\n", (unsigned long)dwell_ms,
                CHANNEL_MIN, CHANNEL_MAX, back);
  for (uint8_t ch = CHANNEL_MIN; ch <= CHANNEL_MAX; ch++) {
    const char *reason = apply_channel(ch);
    if (reason != nullptr) {
      note_printf("# bscan ch=%u err=%s\n", ch, reason);
      continue;
    }
    portENTER_CRITICAL(&g_beacons_mux);
    g_beacons_used = 0;
    g_beacons_overflow = 0;
    g_channel_for_scan = ch;
    g_bscan_active = true;
    portEXIT_CRITICAL(&g_beacons_mux);
    csi_boot::wait_ms(dwell_ms);  // 待つ間もウォッチドッグに知らせる
    static BeaconStat copy[BEACON_SLOTS];  // スタックに置かない（約 1.6 KB）
    portENTER_CRITICAL(&g_beacons_mux);
    g_bscan_active = false;
    const size_t used = g_beacons_used;
    const uint32_t overflow = g_beacons_overflow;
    memcpy(copy, g_beacons, sizeof(BeaconStat) * used);
    portEXIT_CRITICAL(&g_beacons_mux);
    for (size_t i = 0; i < used; i++) {
      const BeaconStat &b = copy[i];
      char ssid_hex[BEACON_SSID_MAX * 2 + 1] = "";
      for (size_t k = 0; k < b.ssid_len; k++) snprintf(ssid_hex + 2 * k, 3, "%02x", b.ssid[k]);
      note_printf("# beacon ch=%u bssid=%02x%02x%02x%02x%02x%02x ap_ch=%u n=%lu csi=%lu "
                    "sig_mode=%u rate=%u ht_cap=%u interval_tu=%u rssi_mean_dbm=%ld ssid_hex=%s\n",
                    ch, b.bssid[0], b.bssid[1], b.bssid[2], b.bssid[3], b.bssid[4], b.bssid[5],
                    b.ap_channel, (unsigned long)b.n, (unsigned long)b.n_csi, b.sig_mode, b.rate,
                    b.ht_cap, b.interval_tu, b.n ? (long)(b.rssi_sum / (int32_t)b.n) : 0L, ssid_hex);
    }
    note_printf("# bscan ch=%u dwell_ms=%lu sources=%u overflow=%lu\n", ch,
                  (unsigned long)dwell_ms, (unsigned)used, (unsigned long)overflow);
  }
  const char *reason = apply_channel(back);
  led_start(LED_SWITCH, LED_EVENT);
  note_printf("# bscan done back_to=%u result=%s\n", g_channel, reason == nullptr ? "ok" : reason);
}

// ALLCSI [ON|OFF]: 送信機以外から来た CSI も出す（テスト用）。引数なしは今の状態を返す。
static void command_all_csi(const char *arg) {
  if (arg == nullptr) {
    note_printf("# ack cmd=ALLCSI on=%u rows=%lu channel=%u\n", g_all_csi ? 1u : 0u,
                  (unsigned long)g_all_csi_rows, g_channel);
    return;
  }
  if (strcmp(arg, "ON") == 0) {
#if CSI_SD
    if (sd_log::recording()) {  // 他の送信元の行を記録に混ぜない
      note_printf("# err cmd=ALLCSI reason=recording\n");
      return;
    }
#endif
    g_all_csi_rows = 0;
    g_all_csi = true;
  } else if (strcmp(arg, "OFF") == 0) {
    g_all_csi = false;
  } else {
    note_printf("# err cmd=ALLCSI reason=bad_arg arg=%s (ON or OFF)\n", arg);
    return;
  }
  note_printf("# ack cmd=ALLCSI on=%u rows=%lu channel=%u\n", g_all_csi ? 1u : 0u,
                (unsigned long)g_all_csi_rows, g_channel);
}

static void command_who() {
  uint8_t mac[6] = {};
  esp_wifi_get_mac(WIFI_IF_STA, mac);
  note_printf("# who role=rx mac=%02x%02x%02x%02x%02x%02x channel=%u format=%u "
                "tx_mac=%02x%02x%02x%02x%02x%02x relay=1 pico=%d auth=%s tx_power_dbm=%.2f "
                "fw=%s paired=%u\n",
                mac[0], mac[1], mac[2], mac[3], mac[4], mac[5], g_channel, FORMAT_VERSION,
                g_tx_mac[0], g_tx_mac[1], g_tx_mac[2], g_tx_mac[3], g_tx_mac[4], g_tx_mac[5],
                pico_connected() ? 1 : 0, g_auth ? "on" : "off_no_key", tx_power_dbm(),
                CSI_FW_VERSION, g_paired ? 1u : 0u);
}

// 組（送信機の MAC と鍵）を NVS から読む（setup_radio の前に 1 回）。未設定なら無線の命令を送らない。
static void setup_pair() {
  const csi_pair::Pair pair = csi_pair::load();
  g_paired = pair.ok;
  if (pair.ok) {
    memcpy(g_tx_mac, pair.peer, sizeof(g_tx_mac));
    memcpy(g_key, pair.key, sizeof(g_key));
  }
  g_auth = pair.ok;
}

// PAIRSET <送信機の MAC> <鍵>: NVS に置いて再起動する。応答に鍵は出さない。
static void command_pair_set(char *arg) {
  const size_t len = strlen(arg);  // store が区切りに NUL を入れるので、先に測る
  const char *reason = csi_pair::store(arg);
  memset(arg, 0, len);  // 命令の受け口に鍵を残さない
  if (reason != nullptr) {
    note_printf("# err cmd=PAIRSET reason=%s\n", reason);
    return;
  }
  note_printf("# ack cmd=PAIRSET restarting\n");
  csi_pair::restart();
}

static void command_pair_clear() {
  const char *reason = csi_pair::clear();
  if (reason != nullptr) {
    note_printf("# err cmd=PAIRCLEAR reason=%s\n", reason);
    return;
  }
  note_printf("# ack cmd=PAIRCLEAR restarting\n");
  csi_pair::restart();
}

// 送信機の出力を無線で変える。成功なら nullptr（*actual_qdbm に実際の値）、失敗なら理由。
// *reverted は lost_link のとき、送信機が元の出力に戻ったのを確かめられたか。
static const char *relay_txp(uint8_t qdbm, int *actual_qdbm, uint32_t *attempts_out,
                             bool *reverted) {
  static uint32_t s_token = 0x80000000u;  // SET_CH の番号と重ならないように
  *attempts_out = 0;
  *actual_qdbm = -1;
  *reverted = false;
  if (!g_auth) return "no_key";
  if (!g_tx_nonce_known) return "no_nonce";
  const uint32_t token = ++s_token;
  const uint32_t old_nonce = g_tx_nonce;
  g_relay_reply = 0;
  g_relay_nonce = old_nonce;
  g_relay_token = token;
  uint32_t attempts = 0;
  while (attempts < RELAY_ATTEMPTS && g_relay_reply == 0) {
    attempts++;
    send_ctrl(CTRL_SET_TXP, qdbm, token, old_nonce);
    const uint32_t t0 = millis();
    while (g_relay_reply == 0 && millis() - t0 < RELAY_ACK_TIMEOUT_MS) csi_boot::wait_ms(1);
  }
  *attempts_out = attempts;
  const uint8_t reply = g_relay_reply;
  if (reply == CTRL_NACK) return "tx_nack";
  if (reply == CTRL_TXP_ACK) *actual_qdbm = g_relay_value;
  // 新しい出力の電波（新しい合言葉が載ったデータのパケット）が届くのを待つ
  const uint32_t start = g_frames;
  const uint32_t t0 = millis();
  bool seen = false;
  while (millis() - t0 < TXP_LINK_WAIT_MS) {
    if (g_tx_nonce != old_nonce && g_frames - start >= TXP_LINK_MIN_FRAMES) {
      seen = true;
      break;
    }
    csi_boot::wait_ms(1);
  }
  if (!seen) {
    if (reply == 0 && g_frames - start >= TXP_LINK_MIN_FRAMES) return "tx_no_ack";  // 命令が届いていない
    // 送信機は元の出力に戻るはず。戻ったか（また届くか）を確かめる
    csi_boot::wait_ms(TXP_CONFIRM_MS + TXP_REVERT_MARGIN_MS - (millis() - t0));
    *reverted = link_alive(LINK_CHECK_MS, LINK_MIN_FRAMES);
    return "lost_link";
  }
  const uint32_t new_nonce = g_tx_nonce;
  for (uint32_t i = 0; i < TXP_CONFIRM_REPEAT; i++) {
    send_ctrl(CTRL_TXP_CONFIRM, qdbm, token, new_nonce);
    csi_boot::wait_ms(TXP_CONFIRM_INTERVAL_MS);
  }
  if (*actual_qdbm < 0 && g_peer_qdbm_known) *actual_qdbm = g_peer_qdbm;  // 了解だけ失われた
  return nullptr;
}

// シリアルの TXPS 命令: 送信機の出力の上限を無線で変える。
static void command_remote_tx_power(const char *arg) {
  char *end = nullptr;
  const float dbm = strtof(arg, &end);
  if (end == arg || *end != '\0' || dbm < TX_POWER_MIN_DBM || dbm > TX_POWER_MAX_DBM) {
    note_printf("# err cmd=TXPS reason=out_of_range arg=%s min=%.0f max=%.0f\n", arg,
                  TX_POWER_MIN_DBM, TX_POWER_MAX_DBM);
    return;
  }
  int actual = -1;
  uint32_t attempts = 0;
  bool reverted = false;
  const char *reason =
      relay_txp(static_cast<uint8_t>(lroundf(dbm * QDBM_PER_DBM)), &actual, &attempts, &reverted);
  if (reason == nullptr) {
    led_start(LED_SWITCH, LED_EVENT);
    note_printf("# ack cmd=TXPS want_dbm=%.2f dbm=%.2f attempts=%lu\n", dbm,
                  actual / QDBM_PER_DBM, (unsigned long)attempts);
    return;
  }
  led_start(LED_ALERT, LED_ERROR);
  if (strcmp(reason, "lost_link") == 0) {
    note_printf("# err cmd=TXPS reason=lost_link want_dbm=%.2f attempts=%lu reverted=%u\n", dbm,
                  (unsigned long)attempts, reverted ? 1u : 0u);
  } else {
    note_printf("# err cmd=TXPS reason=%s want_dbm=%.2f attempts=%lu\n", reason, dbm,
                  (unsigned long)attempts);
  }
}

// 送信機を無線で再起動する。了解が来たか（acked）と、連番が戻ってまた届いたか（back）を返す。
static const char *relay_reboot(bool *acked, bool *back, uint32_t *attempts_out) {
  static uint32_t s_token = 0x40000000u;  // SET_CH・SET_TXP の番号と重ならないように
  *acked = false;
  *back = false;
  *attempts_out = 0;
  if (!g_auth) return "no_key";
  if (!g_tx_nonce_known) return "no_nonce";
  const uint32_t token = ++s_token;
  const uint32_t nonce = g_tx_nonce;
  const uint32_t restarts = g_tx_restarts;
  g_relay_reply = 0;
  g_relay_nonce = nonce;
  g_relay_token = token;
  uint32_t attempts = 0;
  while (attempts < RELAY_ATTEMPTS && g_relay_reply == 0) {
    attempts++;
    send_ctrl(CTRL_REBOOT, 0, token, nonce);
    const uint32_t t0 = millis();
    while (g_relay_reply == 0 && millis() - t0 < RELAY_ACK_TIMEOUT_MS) csi_boot::wait_ms(1);
  }
  *attempts_out = attempts;
  *acked = g_relay_reply == CTRL_ACK;
  const uint32_t t0 = millis();
  while (millis() - t0 < TX_REBOOT_WAIT_MS) {
    if (g_tx_restarts != restarts) {
      *back = true;
      break;
    }
    csi_boot::wait_ms(1);
  }
  if (*back) return nullptr;
  return *acked ? "no_return" : "tx_no_ack";
}

static void command_tx_reboot() {
  bool acked = false;
  bool back = false;
  uint32_t attempts = 0;
  const char *reason = relay_reboot(&acked, &back, &attempts);
  if (reason == nullptr) {
    led_start(LED_SWITCH, LED_EVENT);
    note_printf("# ack cmd=TXREBOOT acked=%u back=1 attempts=%lu\n", acked ? 1u : 0u,
                  (unsigned long)attempts);
  } else {
    led_start(LED_ALERT, LED_ERROR);
    note_printf("# err cmd=TXREBOOT reason=%s acked=%u back=0 attempts=%lu\n", reason,
                  acked ? 1u : 0u, (unsigned long)attempts);
  }
}

// シリアルの TXP 命令: 送信出力の上限を変えて読み戻す（受信機が送るのは制御パケットだけ）。
static void command_tx_power(const char *arg) {
  char *end = nullptr;
  const float dbm = strtof(arg, &end);
  if (end == arg || *end != '\0' || dbm < TX_POWER_MIN_DBM || dbm > TX_POWER_MAX_DBM) {
    note_printf("# err cmd=TXP reason=out_of_range arg=%s min=%.0f max=%.0f\n", arg,
                  TX_POWER_MIN_DBM, TX_POWER_MAX_DBM);
    return;
  }
  const esp_err_t err =
      esp_wifi_set_max_tx_power(static_cast<int8_t>(lroundf(dbm * QDBM_PER_DBM)));
  if (err != ESP_OK) {
    note_printf("# err cmd=TXP reason=%s dbm=%.2f\n", esp_err_to_name(err), dbm);
    return;
  }
  note_printf("# ack cmd=TXP want_dbm=%.2f dbm=%.2f\n", dbm, tx_power_dbm());
}

#if CSI_SD
// ---- SD カードへの保存（ボタン・命令・時刻） ----------------------------------------
// ボタン（firmware/shared/CsiLcd/src/CsiButtons.h）: A = 新しいセッション、B = 止める、C = 印、C 長押し = 明るさ
constexpr size_t SD_HEADER_MAX = 1536;    // セッションの見出し [byte]
constexpr uint32_t SD_ROTATE_SEC_DEFAULT = 3600;  // ファイルを切り替える間隔 [sec]（約 120 MB）
constexpr uint32_t SD_ROTATE_SEC_MIN = 10;        // SDROTATE で受け付ける最短 [sec]（0 = 切り替えない）
constexpr uint32_t SD_ROTATE_SEC_MAX = 86400;     // SDROTATE で受け付ける最長 [sec]
constexpr uint32_t SD_ROTATE_RETRY_MS = 1000;     // 切り替えを頼めなかったときに次に試すまで [ms]
constexpr uint32_t PAUSE_SETTLE_MS = 20;  // シリアルを止めてから、書きかけの行が出終わるまで [ms]
constexpr uint32_t MARK_LABEL_MAX = 24;

static SemaphoreHandle_t g_bus_lock = nullptr;  // 液晶と SD が共有する SPI バスの鍵
static bool g_time_known = false;
static int64_t g_unix_ms_at_boot = 0;   // millis() == 0 のときの unix 時刻 [ms]
static int32_t g_tz_sec = 0;            // UTC からの差 [sec]
static const char *g_time_source = "none";  // none / ntp / serial

// ---- 受信機の NVS に残す設定（再起動しても消えない） ----
constexpr const char *PREFS_NS = "csi";
constexpr const char *PREF_SSID = "ssid";
constexpr const char *PREF_PASS = "pass";
constexpr const char *PREF_TZ = "tz";
constexpr const char *PREF_REC = "rec";        // 1 = セッション中（再起動したら再開する）

// ---- 時刻（NTP） ----
constexpr size_t SSID_MAX = 32;  // [byte]（IEEE 802.11 の上限）
constexpr size_t PASS_MAX = 63;  // [byte]（WPA2 の上限）
constexpr uint32_t NTP_JOIN_TIMEOUT_MS = 15000;  // 手元の Wi-Fi に接続するまで待つ時間 [ms]
constexpr uint32_t NTP_SYNC_TIMEOUT_MS = 10000;  // NTP の答えを待つ時間 [ms]
constexpr uint32_t NTP_LEAVE_TIMEOUT_MS = 1000;  // 切断を待つ時間 [ms]
constexpr const char *NTP_SERVER_1 = "ntp.nict.jp";
constexpr const char *NTP_SERVER_2 = "time.google.com";
constexpr int32_t DEFAULT_TZ_SEC = 9 * 3600;  // 日本
constexpr long long MIN_UNIX = 1700000000LL;  // 2023 年より前の値は誤りとみなす [sec]
constexpr long MAX_TZ_SEC = 14 * 3600;

static void pref_put_u8(const char *key, uint8_t value) {
  Preferences p;
  if (!p.begin(PREFS_NS, false)) {
    note_printf("# err cmd=prefs reason=open key=%s\n", key);
    return;
  }
  if (p.putUChar(key, value) != 1) note_printf("# err cmd=prefs reason=write key=%s\n", key);
  p.end();
}

static uint8_t pref_get_u8(const char *key, uint8_t fallback) {
  Preferences p;
  if (!p.begin(PREFS_NS, true)) return fallback;  // まだ何も書いていなければ開けない
  const uint8_t v = p.getUChar(key, fallback);
  p.end();
  return v;
}

// 今の unix 時刻 [sec]（分からなければ 0）。SD の出来事に付ける
static int64_t unix_now_sec() {
  if (!g_time_known) return 0;
  return (g_unix_ms_at_boot + static_cast<int64_t>(millis())) / 1000;
}

static void apply_time(int64_t unix_ms, int32_t tz_sec, const char *source) {
  g_unix_ms_at_boot = unix_ms - static_cast<int64_t>(millis());
  g_tz_sec = tz_sec;
  g_time_known = true;
  g_time_source = source;
}

// 見出しを文字の列に集める（print_header を SD の見出しに使う）。
// 記録の条件を 1 行で書く（セッションと切り替えたファイルの先頭。晩ごとの違いを比べるため・2026-10-10）:
// 温度（自分・送信機）、液晶の明るさ（SPI を SD と共有）、カードの種類・容量・空き、電源 IC の状態
static void print_env(Print &out) {
  const sd_log::CardInfo card = sd_log::card_info();
  const sd_log::Status st = sd_log::status();
  char peer_temp[8] = "nan";
  if (g_peer_temp_known) snprintf(peer_temp, sizeof(peer_temp), "%d", (int)g_peer_temp_c);
  out.printf("# env temp_c=%.1f peer_temp_c=%s card_type=%s card_mb=%llu free_mb=%llu", g_temp_c,
             peer_temp, card.type, (unsigned long long)card.size_mb,
             (unsigned long long)(st.free_bytes / (1024ULL * 1024ULL)));
#if CSI_M5_LCD
  const csi_power::Status pw = csi_power::read();
  out.printf(" lcd_brightness=%u power_ok=%u charging=%u full=%u batt_pct=%d",
             csi_lcd_brightness(), pw.ok ? 1u : 0u, pw.charging ? 1u : 0u, pw.full ? 1u : 0u,
             pw.ok ? pw.level_pct : -1);
#endif
  out.print("\n");
}

class HeaderBuf : public Print {
 public:
  size_t write(uint8_t c) override {
    if (len_ + 1 >= sizeof(buf_)) return 0;
    buf_[len_++] = static_cast<char>(c);
    return 1;
  }
  const char *data() const { return buf_; }
  size_t size() const { return len_; }

 private:
  char buf_[SD_HEADER_MAX] = {};
  size_t len_ = 0;
};

// 時刻が分かっていれば "2026-10-07T14:00:00+09:00" を out に書いて true。
static bool format_now_iso(char *out, size_t n) {
  if (!g_time_known) return false;
  const time_t local =
      static_cast<time_t>((g_unix_ms_at_boot + static_cast<int64_t>(millis())) / 1000 + g_tz_sec);
  struct tm tmv;
  gmtime_r(&local, &tmv);
  char base[24];
  strftime(base, sizeof(base), "%Y-%m-%dT%H:%M:%S", &tmv);
  const int32_t a = g_tz_sec < 0 ? -g_tz_sec : g_tz_sec;
  snprintf(out, n, "%s%c%02ld:%02ld", base, g_tz_sec < 0 ? '-' : '+', (long)(a / 3600),
           (long)(a / 60 % 60));
  return true;
}

// ---- SD の立て直し（2026-10-08） ----
// 書き込みに失敗したら（sd_log::failed）、打ち切って ① そのまま新しいセッションを開く → ② カードを付け直して開く
// → ③ RECOVER_RETRY_MS ごとに ② を繰り返す（カードが抜けている間は待ち続ける）。新しいセッションの先頭に
// # sd recover … を書き、NVS に出来事 recover を残す。2026-10-07 の夜は、一度失敗した後に立て直さず一晩分を失った
constexpr uint32_t RECOVER_RETRY_MS = 5000;  // 付け直しを繰り返す間隔 [ms]
static bool g_sd_recovering = false;         // 記録したいのに書けていない（立て直し中）
static uint32_t g_sd_recover_next_ms = 0;    // 次に試す時刻 [ms]（millis）
static uint32_t g_sd_recover_tries = 0;      // この立て直しで試した回数
static uint32_t g_sd_recover_since_ms = 0;   // 書けなくなった時刻 [ms]（millis）
static uint32_t g_sd_recoveries = 0;         // 起動から立て直した回数
static char g_sd_lost_name[sd_log::NAME_LEN] = "";      // 書けなくなったセッション
static char g_sd_lost_reason[sd_log::ERROR_LEN] = "";   // その最初の失敗の理由
static int g_sd_lost_errno = 0;
static long g_sd_lost_at_s = -1;             // そのセッションの開始から失敗までの秒（-1 = 不明）
// 打ち切った時点の状態。立て直した後の # sd recover に詳細を書く（2026-10-10: # sd fail は打ち切る
// セッションに書こうとして捨てられることがあり、10-09 の晩は 9 回中 3 回しかファイルに残らなかった。
// NVS の出来事は 16 件の輪で、一晩で古いものが消えた）
static sd_log::Status g_sd_lost_status = {};

// セッションを開く（見出しを書く）。成功なら true。
static bool sd_open_session(const char *source) {
  g_mark_count = 0;
  HeaderBuf h;
  char iso[40];
  if (format_now_iso(iso, sizeof(iso))) h.printf("# capture start=%s\n", iso);
  h.printf("# sd session source=%s uptime_ms=%lu time_known=%u time_source=%s fw=%s\n", source,
           (unsigned long)millis(), g_time_known ? 1u : 0u, g_time_source, CSI_FW_VERSION);
  print_header(h);
  print_env(h);
  char name[sd_log::NAME_LEN];
  if (!sd_log::start(h.data(), h.size(), name, sizeof(name))) {
    led_start(LED_ALERT, LED_ERROR);
    note_printf("# err cmd=SDSTART source=%s reason=%s\n", source, sd_log::status().last_error);
    return false;
  }
  led_start(LED_SWITCH, LED_EVENT);
  pref_put_u8(PREF_REC, 1);  // 再起動したら再開する
  note_printf("# sd start name=%s source=%s\n", name, source);  // ファイルにも入る
  return true;
}

static void sd_start(const char *source) {
  if (g_sd_recovering) {  // 立て直し中なら、すぐにもう一度試す
    g_sd_recover_next_ms = millis();
    note_printf("# sd retry source=%s\n", source);
    return;
  }
  if (sd_log::recording()) {
    note_printf("# err cmd=SDSTART reason=already_recording name=%s\n", sd_log::status().name);
    return;
  }
  if (g_all_csi) {  // 他の送信元の行を記録に混ぜない
    note_printf("# err cmd=SDSTART reason=all_csi (ALLCSI OFF first)\n");
    return;
  }
  sd_open_session(source);
}

static uint32_t g_sd_rotate_sec = SD_ROTATE_SEC_DEFAULT;  // ファイルを切り替える間隔 [sec]（0 = 切り替えない）
static uint32_t g_sd_rotate_retry_ms = 0;  // 切り替えを頼めなかったとき、次に試す時刻 [ms]（millis。0 = すぐ）

// loop から毎回呼ぶ: 今のファイルを開いてから g_sd_rotate_sec たったら、次の通し番号のファイルに切り替える。
static void sd_poll_rotate() {
  if (g_sd_rotate_sec == 0 || !sd_log::recording() || sd_log::failed()) return;
  const sd_log::Status st = sd_log::status();
  const uint32_t now = millis();
  if (st.rotate_pending || now - st.file_started_ms < g_sd_rotate_sec * 1000UL) return;
  if (g_sd_rotate_retry_ms != 0 && static_cast<int32_t>(now - g_sd_rotate_retry_ms) < 0) return;
  HeaderBuf h;
  char iso[40];
  if (format_now_iso(iso, sizeof(iso))) h.printf("# capture start=%s\n", iso);
  h.printf("# sd session source=rotate prev=%s part=%lu uptime_ms=%lu time_known=%u "
           "time_source=%s fw=%s\n",
           st.name, (unsigned long)(st.part + 1), (unsigned long)now, g_time_known ? 1u : 0u,
           g_time_source, CSI_FW_VERSION);
  print_header(h);
  print_env(h);
  // 前のファイルの最後の行（印より前に置くので前のファイルに入る）
  note_printf("# sd rotate from=%s part=%lu bytes=%llu total_bytes=%llu\n", st.name,
             (unsigned long)st.part, (unsigned long long)st.bytes,
             (unsigned long long)st.total_bytes);
  if (sd_log::rotate(h.data(), h.size())) {
    g_sd_rotate_retry_ms = 0;
  } else {  // バッファが満杯など。少し待ってもう一度
    note_printf("# err cmd=SDROTATE reason=busy name=%s\n", st.name);
    g_sd_rotate_retry_ms = now + SD_ROTATE_RETRY_MS;
  }
}

static void sd_set_rotate(const char *arg) {
  char *end = nullptr;
  const unsigned long sec = strtoul(arg, &end, 10);
  if (end == arg || *end != '\0' ||
      (sec != 0 && (sec < SD_ROTATE_SEC_MIN || sec > SD_ROTATE_SEC_MAX))) {
    note_printf("# err cmd=SDROTATE reason=bad_arg min=%lu max=%lu\n",
                (unsigned long)SD_ROTATE_SEC_MIN, (unsigned long)SD_ROTATE_SEC_MAX);
    return;
  }
  g_sd_rotate_sec = static_cast<uint32_t>(sec);
  note_printf("# ack cmd=SDROTATE sec=%lu\n", sec);
}

// SDFAULT <n>: 次の n 回のドライバの書き込みをわざと失敗させる（試験用。書き直し・立て直しを確かめる）。
static void sd_fault(const char *arg) {
  char *end = nullptr;
  const long n = strtol(arg, &end, 10);
  if (end == arg || *end != '\0' || n < 0 || n > static_cast<long>(sd_log::FAULT_MAX)) {
    note_printf("# err cmd=SDFAULT reason=bad_count max=%lu\n", (unsigned long)sd_log::FAULT_MAX);
    return;
  }
  sd_log::inject_write_faults(static_cast<uint32_t>(n));
  note_printf("# ack cmd=SDFAULT n=%ld\n", n);
}

// 立て直しを始める（書けなくなったことを記録して、セッションを打ち切る）。
static void sd_begin_recover(const char *reason, int err_no, long at_s) {
  const sd_log::Status st = sd_log::status();
  strncpy(g_sd_lost_name, st.recording ? st.name : "-", sizeof(g_sd_lost_name) - 1);
  strncpy(g_sd_lost_reason, reason, sizeof(g_sd_lost_reason) - 1);
  g_sd_lost_errno = err_no;
  g_sd_lost_at_s = at_s;
  g_sd_lost_status = st;
  note_printf("# sd fail name=%s reason=%s errno=%d at_s=%ld bytes=%llu synced=%llu "
             "status_fail=%lu disk_fail=%lu disk_retry=%lu disk_retry_ok=%lu read_fail=%lu\n",
             g_sd_lost_name, reason, err_no, at_s, (unsigned long long)st.bytes,
             (unsigned long long)st.synced_bytes, (unsigned long)st.status_fail,
             (unsigned long)st.disk_fail, (unsigned long)st.disk_retry,
             (unsigned long)st.disk_retry_ok, (unsigned long)st.read_fail);
  sd_log::abort();
  g_sd_recovering = true;
  g_sd_recover_tries = 0;
  g_sd_recover_since_ms = millis();
  g_sd_recover_next_ms = g_sd_recover_since_ms;
  led_start(LED_ALERT, LED_ERROR);
}

// loop から毎回呼ぶ: 失敗を見つけたら立て直しを始め、時刻が来たら試す。
static void sd_poll_recover() {
  if (!g_sd_recovering) {
    if (!sd_log::failed()) return;
    const sd_log::Status st = sd_log::status();
    const long at_s = st.first_error_ms != 0
                          ? static_cast<long>((st.first_error_ms - st.started_ms) / 1000)
                          : -1L;
    sd_begin_recover(st.first_error, st.first_errno, at_s);
  }
  if (static_cast<int32_t>(millis() - g_sd_recover_next_ms) < 0) return;
  g_sd_recover_tries++;
  int stage = 0;
  // ① 最初の 1 回だけ、付け直さずに開く（ファイルだけが壊れた場合）。② カードを付け直して開く
  if (g_sd_recover_tries == 1 && sd_log::status().mounted && sd_open_session("recover")) {
    stage = 1;
  } else if (sd_log::remount() && sd_open_session("recover")) {
    stage = 2;
  }
  if (stage == 0) {
    g_sd_recover_next_ms = millis() + RECOVER_RETRY_MS;
    return;
  }
  const uint32_t down_ms = millis() - g_sd_recover_since_ms;
  g_sd_recovering = false;
  g_sd_recoveries++;
  // 新しいセッションの先頭（見出しの後）に経緯を書く
  // 後半は打ち切った時点の状態: bytes/synced = 書いたつもりの量とカード上の長さ、disk_* = ドライバの書き込み
  // （失敗の回数・書き直し・最後の失敗の時間・カードの応答・セクタ）、status_fail/read_fail
  const sd_log::Status &ls = g_sd_lost_status;
  note_printf("# sd recover from=%s reason=%s errno=%d failed_at_s=%ld stage=%d tries=%lu "
             "down_ms=%lu recoveries=%lu bytes=%llu synced=%llu disk_writes=%lu disk_fail=%lu "
             "disk_retry=%lu disk_retry_ok=%lu disk_fail_ms=%lu disk_fail_card_ok=%d "
             "disk_fail_sector=%lu disk_fail_count=%lu disk_max_ms=%lu status_fail=%lu "
             "read_fail=%lu\n",
             g_sd_lost_name, g_sd_lost_reason, g_sd_lost_errno, g_sd_lost_at_s, stage,
             (unsigned long)g_sd_recover_tries, (unsigned long)down_ms,
             (unsigned long)g_sd_recoveries, (unsigned long long)ls.bytes,
             (unsigned long long)ls.synced_bytes, (unsigned long)ls.disk_writes,
             (unsigned long)ls.disk_fail, (unsigned long)ls.disk_retry,
             (unsigned long)ls.disk_retry_ok, (unsigned long)ls.disk_fail_ms,
             (int)ls.disk_fail_card_ok, (unsigned long)ls.disk_fail_sector,
             (unsigned long)ls.disk_fail_count, (unsigned long)ls.disk_write_max_ms,
             (unsigned long)ls.status_fail, (unsigned long)ls.read_fail);
  sd_log::record_event(sd_log::EventKind::RECOVER, static_cast<int16_t>(stage), down_ms, down_ms,
                       g_sd_recover_tries);
}

static void sd_stop(const char *source) {
  if (g_sd_recovering) {  // 立て直しをやめて止める
    g_sd_recovering = false;
    pref_put_u8(PREF_REC, 0);
    note_printf("# sd stop name=%s source=%s recovering=1 tries=%lu\n", g_sd_lost_name, source,
               (unsigned long)g_sd_recover_tries);
    led_start(LED_SWITCH, LED_EVENT);
    return;
  }
  if (!sd_log::recording()) {
    note_printf("# err cmd=SDSTOP reason=not_recording\n");
    return;
  }
  const sd_log::Status st = sd_log::status();
  note_printf("# sd stop name=%s source=%s bytes=%llu total_bytes=%llu part=%lu dropped_lines=%lu "
             "write_max_ms=%lu flush_max_ms=%lu rotate_max_ms=%lu buffer_high=%lu\n",
             st.name, source, (unsigned long long)st.bytes, (unsigned long long)st.total_bytes,
             (unsigned long)st.part, (unsigned long)st.dropped_lines,
             (unsigned long)st.write_max_ms, (unsigned long)st.flush_max_ms,
             (unsigned long)st.rotate_max_ms, (unsigned long)st.buffer_high);  // ファイルの最後の行
  sd_log::stop();
  pref_put_u8(PREF_REC, 0);
  led_start(LED_SWITCH, LED_EVENT);
}

static void sd_mark(const char *label, const char *source) {
  // 記録中でなければ行は SD に残らない（シリアルには出る）。音と画面で区別して知らせる
  const bool recorded = sd_log::recording();
  if (recorded) ++g_mark_count;
#if CSI_M5_LCD
  // 音は行を書く前に鳴らし始める（シリアルの書き出しを待つ分だけ音が遅れないように）
  mark_feedback(recorded);
  const uint32_t beep_hz = recorded ? BEEP_MARK_HZ : BEEP_NOT_REC_HZ;
#else
  const uint32_t beep_hz = 0;
#endif
  note_printf("# mark label=%s seq=%lld rx_us=%lu source=%s n=%lu recording=%u beep_hz=%lu\n", label,
             (long long)g_last_seq_out, (unsigned long)g_last_rx_us, source,
             (unsigned long)g_mark_count, recorded ? 1u : 0u, (unsigned long)beep_hz);
  led_start(LED_SWITCH, LED_EVENT);
}

static void sd_info() {
  const sd_log::Status st = sd_log::status();
  note_printf("# sd info mounted=%u recording=%u name=%s bytes=%llu total_bytes=%llu part=%lu "
             "rotate_sec=%lu rotate_max_ms=%lu dropped_lines=%lu "
             "write_max_ms=%lu flush_max_ms=%lu buffer_high=%lu errors=%lu last_error=%s "
             "free_mb=%llu time_known=%u time_source=%s resume=%u\n",
             st.mounted ? 1u : 0u, st.recording ? 1u : 0u, st.name[0] ? st.name : "-",
             (unsigned long long)st.bytes, (unsigned long long)st.total_bytes,
             (unsigned long)st.part, (unsigned long)g_sd_rotate_sec,
             (unsigned long)st.rotate_max_ms, (unsigned long)st.dropped_lines,
             (unsigned long)st.write_max_ms, (unsigned long)st.flush_max_ms,
             (unsigned long)st.buffer_high, (unsigned long)st.errors,
             st.last_error[0] ? st.last_error : "-",
             (unsigned long long)(st.free_bytes / (1024ULL * 1024ULL)), g_time_known ? 1u : 0u,
             g_time_source, pref_get_u8(PREF_REC, 0));
}

static void feed_watchdog() {
  if (csi_boot::watchdog_on()) esp_task_wdt_reset();
}

static void sd_get(const char *arg) {
  char name[sd_log::NAME_LEN] = {};
  unsigned long offset = 0;
  if (sscanf(arg, "%15s %lu", name, &offset) < 1) {
    note_printf("# err cmd=SDGET reason=bad_arg\n");
    return;
  }
  if (sd_log::recording()) {
    note_printf("# err cmd=SDGET reason=recording\n");
    return;
  }
  g_serial_paused = true;  // CSI 行と # 行を止める（書きかけの行は出し終わるのを待つ）
  delay(PAUSE_SETTLE_MS);
  Serial.flush();
  const int64_t sent = sd_log::send(name, static_cast<uint32_t>(offset), Serial, feed_watchdog);
  Serial.flush();
  g_serial_paused = false;
  if (sent < 0) note_printf("# err cmd=SDGET reason=cannot_read name=%s\n", name);
}

static void sd_ls() {
  if (!sd_log::list(Serial)) {
    note_printf("# err cmd=SDLS reason=%s\n", sd_log::recording() ? "recording" : "no_card");
    return;
  }
  note_printf("# sd ls done\n");
}

constexpr uint64_t BYTES_PER_MIB = 1024ULL * 1024ULL;  // [byte]（SDRM の応答の単位）

// 消した後に、ファイルの大きさと消す前後の空き [MiB] を返す（freed_mb = 増えた空き。bytes より明らかに
// 少なければ、空きの数えがずれている。2026-10-10）
static void sd_rm(const char *name) {
  sd_log::RemoveResult r = {};
  if (!sd_log::remove(name, &r)) {
    note_printf("# err cmd=SDRM reason=%s name=%s\n",
               sd_log::recording() ? "recording" : "cannot_remove", name);
    return;
  }
  const long long freed = static_cast<long long>(r.free_after) - static_cast<long long>(r.free_before);
  note_printf("# ack cmd=SDRM name=%s bytes=%llu free_before_mb=%llu free_after_mb=%llu "
              "freed_mb=%lld\n",
              name, (unsigned long long)r.bytes, (unsigned long long)(r.free_before / BYTES_PER_MIB),
              (unsigned long long)(r.free_after / BYTES_PER_MIB), freed / (long long)BYTES_PER_MIB);
}

static void set_time(const char *arg) {
  long long unix_sec = 0;
  long tz = 0;
  const int n = sscanf(arg, "%lld %ld", &unix_sec, &tz);
  if (n < 1 || unix_sec < MIN_UNIX || tz < -MAX_TZ_SEC || tz > MAX_TZ_SEC) {
    note_printf("# err cmd=TIME reason=bad_arg\n");
    return;
  }
  apply_time(static_cast<int64_t>(unix_sec) * 1000, static_cast<int32_t>(tz), "serial");
  char iso[40];
  format_now_iso(iso, sizeof(iso));
  note_printf("# ack cmd=TIME now=%s\n", iso);
}

static csi_buttons::Buttons g_buttons;

// 起動時に手元の Wi-Fi へつなぎ、NTP で時刻を取って切断する（setup_radio の前に 1 回）。
// 設定が無い・つながらない・答えが無ければ時刻なしで続ける（記録は止めない）。
static void ntp_sync() {
  char ssid[SSID_MAX + 1] = {};
  char pass[PASS_MAX + 1] = {};
  int32_t tz = DEFAULT_TZ_SEC;
  {
    Preferences p;
    if (p.begin(PREFS_NS, true)) {
      if (p.isKey(PREF_SSID)) p.getString(PREF_SSID, ssid, sizeof(ssid));
      if (p.isKey(PREF_PASS)) p.getString(PREF_PASS, pass, sizeof(pass));
      tz = p.getInt(PREF_TZ, DEFAULT_TZ_SEC);
      p.end();
    }
  }
  if (ssid[0] == '\0') {
    note_printf("# ntp ok=0 reason=no_wifi_config (set with WIFISET / sd_fetch.py wifi)\n");
    return;
  }
  const uint32_t t0 = millis();
  WiFi.persistent(false);  // Arduino 側の NVS に接続先を書かない（置き場は PREFS_NS の 1 か所）
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(false);
  WiFi.begin(ssid, pass);
  memset(pass, 0, sizeof(pass));
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < NTP_JOIN_TIMEOUT_MS) delay(10);
  const uint32_t join_ms = millis() - t0;
  const char *reason = nullptr;
  if (WiFi.status() != WL_CONNECTED) {
    reason = "join_timeout";
  } else {
    configTime(0, 0, NTP_SERVER_1, NTP_SERVER_2);
    const uint32_t t1 = millis();
    while (time(nullptr) < MIN_UNIX && millis() - t1 < NTP_SYNC_TIMEOUT_MS) delay(10);
    if (time(nullptr) < MIN_UNIX) reason = "ntp_timeout";
    sntp_stop();  // 切断後に問い合わせ続けない
  }
  struct timeval tv = {};
  gettimeofday(&tv, nullptr);
  WiFi.disconnect(true);
  const uint32_t t2 = millis();
  while (WiFi.status() == WL_CONNECTED && millis() - t2 < NTP_LEAVE_TIMEOUT_MS) delay(10);
  WiFi.mode(WIFI_OFF);  // setup_radio が受信用に起動し直す
  if (reason != nullptr) {
    note_printf("# ntp ok=0 reason=%s join_ms=%lu\n", reason, (unsigned long)join_ms);
    return;
  }
  apply_time(static_cast<int64_t>(tv.tv_sec) * 1000 + tv.tv_usec / 1000, tz, "ntp");
  char iso[40];
  format_now_iso(iso, sizeof(iso));
  note_printf("# ntp ok=1 now=%s join_ms=%lu total_ms=%lu\n", iso, (unsigned long)join_ms,
              (unsigned long)(millis() - t0));
}

// 16 進の文字列を out に戻す。成功なら長さ、失敗なら -1。
static int hex_decode(const char *hex, char *out, size_t out_max) {
  const size_t n = strlen(hex);
  if (n % 2 != 0 || n / 2 > out_max) return -1;
  for (size_t i = 0; i < n / 2; i++) {
    unsigned v = 0;
    if (sscanf(hex + 2 * i, "%2x", &v) != 1) return -1;
    out[i] = static_cast<char>(v);
  }
  out[n / 2] = '\0';
  return static_cast<int>(n / 2);
}

// WIFISET <名前の16進> <パスワードの16進（空なら -）> <UTC からの差 秒>。応答に中身は出さない。
static void command_wifi_set(char *arg) {
  char *save = nullptr;
  const char *ssid_hex = strtok_r(arg, " ", &save);
  const char *pass_hex = strtok_r(nullptr, " ", &save);
  const char *tz_text = strtok_r(nullptr, " ", &save);
  char ssid[SSID_MAX + 1] = {};
  char pass[PASS_MAX + 1] = {};
  const int ssid_len = ssid_hex ? hex_decode(ssid_hex, ssid, SSID_MAX) : -1;
  const bool open = pass_hex != nullptr && strcmp(pass_hex, "-") == 0;
  const int pass_len = open ? 0 : (pass_hex ? hex_decode(pass_hex, pass, PASS_MAX) : -1);
  char *end = nullptr;
  const long tz = tz_text ? strtol(tz_text, &end, 10) : 0;
  const bool tz_ok = tz_text != nullptr && end != tz_text && *end == '\0' && tz >= -MAX_TZ_SEC &&
                     tz <= MAX_TZ_SEC;
  if (ssid_len < 1 || pass_len < 0 || !tz_ok) {
    memset(pass, 0, sizeof(pass));
    note_printf("# err cmd=WIFISET reason=bad_arg\n");
    return;
  }
  Preferences p;
  bool ok = p.begin(PREFS_NS, false);
  if (ok) {
    ok = p.putString(PREF_SSID, ssid) == static_cast<size_t>(ssid_len) &&
         p.putString(PREF_PASS, pass) == static_cast<size_t>(pass_len) &&
         p.putInt(PREF_TZ, static_cast<int32_t>(tz)) == sizeof(int32_t);
    p.end();
  }
  memset(pass, 0, sizeof(pass));
  if (!ok) {
    note_printf("# err cmd=WIFISET reason=nvs\n");
    return;
  }
  note_printf("# ack cmd=WIFISET ssid_len=%d pass_len=%d tz_sec=%ld (used at next boot)\n",
              ssid_len, pass_len, tz);
}

static void command_wifi_clear() {
  Preferences p;
  if (!p.begin(PREFS_NS, false)) {
    note_printf("# err cmd=WIFICLEAR reason=nvs\n");
    return;
  }
  p.remove(PREF_SSID);
  p.remove(PREF_PASS);
  p.end();
  note_printf("# ack cmd=WIFICLEAR\n");
}

// SPI バスの鍵を作って液晶と SD に渡し、カードを取り付け、ボタンを準備する（setup から 1 回）。
static void sd_setup() {
  g_bus_lock = xSemaphoreCreateMutex();
#if CSI_M5_LCD
  csi_lcd_set_bus_lock(g_bus_lock);
#endif
  g_buttons.begin();
  sd_log::set_clock(unix_now_sec);
  const bool ok = g_bus_lock != nullptr && sd_log::begin(g_bus_lock);
  const sd_log::Status st = sd_log::status();
  note_printf("# sd mounted=%u free_mb=%llu reason=%s disk_hooked=%u "
              "buttons=A:start,B:stop,C:mark,C_long:brightness heap_free=%u\n",
             ok ? 1u : 0u, (unsigned long long)(st.free_bytes / (1024ULL * 1024ULL)),
             ok ? "-" : st.last_error, st.disk_hooked ? 1u : 0u, (unsigned)ESP.getFreeHeap());
  note_printf("# lcd brightness=%u\n", csi_lcd_brightness());
  // セッション中に再起動していたら（停電・ウォッチドッグ・リセット）、新しいセッションで続ける
  if (pref_get_u8(PREF_REC, 0) == 1) {
    if (ok) {
      note_printf("# sd resume boot=%s\n", csi_boot::reset_reason_name());
      if (!sd_open_session("resume")) sd_begin_recover("resume_open_failed", 0, -1);
      // 再起動の理由を出来事に残す（brownout なら電源の電圧低下）
      sd_log::record_event(sd_log::EventKind::RESUME, static_cast<int16_t>(esp_reset_reason()));
    } else {
      // 印は残し、カードが差されるまで立て直しの手順で待つ
      note_printf("# err cmd=SDRESUME reason=no_card\n");
      sd_begin_recover("no_card_at_boot", 0, -1);
    }
  }
}

static void poll_buttons() {
  switch (g_buttons.poll()) {
    case csi_buttons::Action::A: sd_start("button"); break;
    case csi_buttons::Action::B: sd_stop("button"); break;
    case csi_buttons::Action::C_SHORT: sd_mark("button", "button"); break;
    case csi_buttons::Action::BRIGHTNESS:
    case csi_buttons::Action::WAKE:
      note_printf("# lcd brightness=%u source=button\n", csi_lcd_brightness());
      break;
    case csi_buttons::Action::NONE: break;
  }
}
#endif

static void handle_command(char *line) {
  if (strncmp(line, "TXP ", 4) == 0) {
    command_tx_power(line + 4);
#if CSI_SD
  } else if (strcmp(line, "SDSTART") == 0) {
    sd_start("serial");
  } else if (strcmp(line, "SDSTOP") == 0) {
    sd_stop("serial");
  } else if (strcmp(line, "SDMARK") == 0 || strncmp(line, "SDMARK ", 7) == 0) {
    char label[MARK_LABEL_MAX + 1] = "serial";
    if (line[6] == ' ') sscanf(line + 7, "%24s", label);
    sd_mark(label, "serial");
  } else if (strcmp(line, "SDLS") == 0) {
    sd_ls();
  } else if (strncmp(line, "SDGET ", 6) == 0) {
    sd_get(line + 6);
  } else if (strncmp(line, "SDRM ", 5) == 0) {
    sd_rm(line + 5);
  } else if (strcmp(line, "SDINFO") == 0) {
    sd_info();
  } else if (strncmp(line, "SDROTATE ", 9) == 0) {
    sd_set_rotate(line + 9);
  } else if (strcmp(line, "SDEVENTS") == 0) {
    sd_log::print_events(Serial, false);
  } else if (strcmp(line, "SDEVENTS CLEAR") == 0) {
    sd_log::print_events(Serial, true);
  } else if (strncmp(line, "SDFAULT ", 8) == 0) {
    sd_fault(line + 8);
  } else if (strncmp(line, "TIME ", 5) == 0) {
    set_time(line + 5);
  } else if (strncmp(line, "WIFISET ", 8) == 0) {
    command_wifi_set(line + 8);
  } else if (strcmp(line, "WIFICLEAR") == 0) {
    command_wifi_clear();
#endif
  } else if (strcmp(line, "TXREBOOT") == 0) {
    command_tx_reboot();
  } else if (strcmp(line, "REBOOT") == 0) {
    note_printf("# ack cmd=REBOOT restarting\n");
    Serial.flush();
    delay(REBOOT_DELAY_MS);
    ESP.restart();
  } else if (strcmp(line, "WDTTEST") == 0) {
    note_printf("# wdt_test block_ms=%lu watchdog=%s\n", (unsigned long)WDT_TEST_BLOCK_MS,
                  csi_boot::watchdog_on() ? "on" : "off");
    Serial.flush();
    delay(WDT_TEST_BLOCK_MS);  // ウォッチドッグが働けば、ここで再起動する
    note_printf("# wdt_test survived (watchdog did not fire)\n");
  } else if (strncmp(line, "TXPS ", 5) == 0) {
    command_remote_tx_power(line + 5);
  } else if (strcmp(line, "PJOIN") == 0) {
    command_pico_join();
  } else if (strcmp(line, "PLEAVE") == 0) {
    command_pico_leave();
  } else if (strncmp(line, "CH ", 3) == 0) {
    command_channel(line + 3);
  } else if (strncmp(line, "CHLOCAL ", 8) == 0) {
    command_channel_local(line + 8);
  } else if (strcmp(line, "OTHERS") == 0) {
    command_others();
  } else if (strcmp(line, "ALLCSI") == 0) {
    command_all_csi(nullptr);
  } else if (strncmp(line, "ALLCSI ", 7) == 0) {
    command_all_csi(line + 7);
  } else if (strcmp(line, "BSCAN") == 0) {
    command_bscan(nullptr);
  } else if (strncmp(line, "BSCAN ", 6) == 0) {
    command_bscan(line + 6);
  } else if (strcmp(line, "WHO") == 0) {
    command_who();
  } else if (strncmp(line, "PAIRSET ", 8) == 0) {
    command_pair_set(line + 8);
  } else if (strcmp(line, "PAIRCLEAR") == 0) {
    command_pair_clear();
#if CSI_M5_LCD
  } else if (strcmp(line, "BATT") == 0) {
    char buf[256];
    csi_power::format(buf, sizeof(buf), "#");  // 電源 IC（IP5306）の状態
    note_printf("%s", buf);
#endif
  } else {
    note_printf("# err cmd=%s reason=unknown\n", line);
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
        note_printf("# err cmd=? reason=too_long max=%u\n", (unsigned)COMMAND_MAX_LEN);
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

// LED: 同期の合図で 2 回点滅。しばらく受けていなければ、ときどき短くちらつく（生きているが受信なし）。
static void update_led() {
  static uint32_t s_last_sync_ms = 0;
  static uint32_t s_last_wait_ms = 0;
  const uint32_t now = millis();
  if (g_led_sync) {
    g_led_sync = false;
    s_last_sync_ms = now;
    led_start(LED_BEAT_RX, LED_NORMAL);
  } else if (now - s_last_sync_ms > LED_NO_SYNC_MS && now - s_last_wait_ms >= LED_WAITING_EVERY_MS) {
    s_last_wait_ms = now;
    led_start(LED_WAITING, LED_IDLE);
  }
  led_update();
}

// ---- 液晶の表示（M5Stack のみ） ---------------------------------------------

#if CSI_M5_LCD
// 液晶: 受信レートの目安 [Hz]（送信機は 100 Hz）。下回ったら注意、0 なら異常の色にする
constexpr float LCD_RATE_WARN_HZ = 50.0f;
constexpr uint32_t SEC_PER_MIN = 60;
constexpr uint32_t SEC_PER_HOUR = 3600;
constexpr double BYTES_PER_MB = 1048576.0;
constexpr double BYTES_PER_GB = 1073741824.0;
constexpr uint64_t LCD_GB_FROM_BYTES = 1000ULL * 1048576ULL;  // これ以上は GB で出す（行に収まるように）

// 量を "45.1M" / "1.23G" の形で書く（液晶の 6 行目。MB = 2^20 byte）。
static void lcd_format_size(char *out, size_t n, uint64_t bytes) {
  if (bytes < LCD_GB_FROM_BYTES) {
    snprintf(out, n, "%.1fM", bytes / BYTES_PER_MB);
  } else {
    snprintf(out, n, "%.2fG", bytes / BYTES_PER_GB);
  }
}

// 1 秒ごとの状況を液晶に渡す。欠け率は直近 1 秒と起動からの通算の両方を出す
// 電源 IC の状態（充電中・満充電・残量）が変わったら # power を書く（1 秒ごとに呼ぶ。2026-10-10）。
// 電池の電圧は読めない（CsiPower.h）。手元の受信機では電池を外しても値が変わらなかったので、目安として扱う
static void poll_power() {
  static bool s_have = false;
  static csi_power::Status s_last = {};
  const csi_power::Status pw = csi_power::read();
  if (s_have && pw.ok == s_last.ok && pw.charging == s_last.charging && pw.full == s_last.full &&
      pw.level_pct == s_last.level_pct) {
    return;
  }
  note_printf("# power ok=%u charging=%u full=%u batt_pct=%d source=%s\n", pw.ok ? 1u : 0u,
              pw.charging ? 1u : 0u, pw.full ? 1u : 0u, pw.ok ? pw.level_pct : -1,
              s_have ? "change" : "start");
  s_last = pw;
  s_have = true;
}

static CsiLcdStatus g_lcd_last = {};  // post_lcd が最後に作った内容（印のときの描き直しに使う）
static bool g_lcd_have = false;

// 印を付けて MARK_FLASH_MS の間は、状態の帯を印の表示に変える
static void apply_mark_overlay(CsiLcdStatus &st) {
  if (static_cast<int32_t>(g_mark_flash_until_ms - millis()) <= 0) return;
  if (g_mark_flash_recorded) {
    st.level = CSI_LCD_WARN;  // 黄色の帯で目立たせる
    snprintf(st.state, sizeof(st.state), "MARK %lu OK", (unsigned long)g_mark_count);
  } else {
    st.level = CSI_LCD_ERROR;
    snprintf(st.state, sizeof(st.state), "MARK: NOT REC");
  }
}

// 印を付けた: 音を鳴らし、状態の帯をすぐ描き直す（次の 1 秒ごとの描画を待たない）
static void mark_feedback(bool recorded) {
  speaker_beep(recorded ? BEEP_MARK_HZ : BEEP_NOT_REC_HZ, recorded ? BEEP_MARK_MS : BEEP_NOT_REC_MS);
  g_mark_flash_recorded = recorded;
  g_mark_flash_until_ms = millis() + MARK_FLASH_MS;
  if (!g_lcd_have) return;
  CsiLcdStatus st = g_lcd_last;
  apply_mark_overlay(st);
  csi_lcd_post(st);
}

static void post_lcd(float rate_hz) {
  static uint32_t s_seen = 0;
  static uint32_t s_missing = 0;
  const uint32_t seen = g_seq_seen;
  const uint32_t missing = g_seq_missing;
  const uint32_t d_seen = seen - s_seen;
  const uint32_t d_missing = missing - s_missing;
  s_seen = seen;
  s_missing = missing;
  const float loss_1s = d_seen + d_missing > 0 ? 100.0f * d_missing / (d_seen + d_missing) : 0.0f;
  const float loss_all = seen + missing > 0 ? 100.0f * missing / (seen + missing) : 0.0f;

  CsiLcdStatus st = {};
  if (!g_paired) {
    st.level = CSI_LCD_WARN;  // 送信機が決まっていないので何も拾わない（csikit flash で組む）
    snprintf(st.state, sizeof(st.state), "NOT PAIRED");
  } else if (rate_hz <= 0.0f) {
    st.level = CSI_LCD_ERROR;
    snprintf(st.state, sizeof(st.state), "NO SIGNAL");
  } else if (rate_hz < LCD_RATE_WARN_HZ) {
    st.level = CSI_LCD_WARN;
    snprintf(st.state, sizeof(st.state), "LOW RATE");
  } else {
    st.level = CSI_LCD_OK;
    snprintf(st.state, sizeof(st.state), "RX from %s", CSI_PEER_LABEL);
#if CSI_SD
    // SD を立て直したことがあれば回数を出す（朝に画面で分かるように）
    if (g_sd_recoveries > 0) {
      snprintf(st.state, sizeof(st.state), "RX from %s R%lu", CSI_PEER_LABEL,
               (unsigned long)g_sd_recoveries);
    }
#endif
  }
  uint8_t mac[6] = {};
  esp_wifi_get_mac(WIFI_IF_STA, mac);
  snprintf(st.lines[0], sizeof(st.lines[0]), "%02x:%02x:%02x:%02x:%02x:%02x", mac[0], mac[1],
           mac[2], mac[3], mac[4], mac[5]);
  snprintf(st.lines[1], sizeof(st.lines[1]), "CH %u   %.1f Hz", g_channel, rate_hz);
#if CSI_SD
  // 2 行目の右端: このセッションで記録した印の数（押せたかを後から確かめられるように・2026-10-11）
  if (sd_log::recording() && g_mark_count > 0) {
    snprintf(st.lines_right[1], sizeof(st.lines_right[1]), "MARK %lu", (unsigned long)g_mark_count);
  }
#endif
  snprintf(st.lines[2], sizeof(st.lines[2]), "loss %.1f%%  all %.1f%%", loss_1s, loss_all);
  if (rate_hz > 0.0f) {
    snprintf(st.lines[3], sizeof(st.lines[3]), "RSSI %d  %s %.1fdBm", (int)g_last_rssi_dbm,
             CSI_PEER_LABEL, g_peer_qdbm_known ? g_peer_qdbm / QDBM_PER_DBM : 0.0f);
  } else {
    snprintf(st.lines[3], sizeof(st.lines[3]), "RSSI --  %s --", CSI_PEER_LABEL);
  }
  snprintf(st.lines[4], sizeof(st.lines[4]), "drop %lu  relay %lu/%lu", (unsigned long)g_dropped,
           (unsigned long)g_relay_ok, (unsigned long)g_relay_err);
#if CSI_SD
  // 6 行目: SD の状態（記録中 = REC と名前・量、取りこぼしがあれば DROP、カードに書けなければ SD ERR）。
  // 記録中は 5 行目の右端に経過時間（HH:MM:SS）を出し、経過と 6 行目を赤字にする（2026-10-08: 量が増えると
  // 経過が 6 行目からはみ出していた）
  const sd_log::Status sd = sd_log::status();
  if (g_sd_recovering) {
    // 立て直し中: 試した回数と、書けなくなった理由
    snprintf(st.lines[5], sizeof(st.lines[5]), "SD RETRY %lu %.12s", (unsigned long)g_sd_recover_tries,
             g_sd_lost_reason);
    st.line_fg[5] = CSI_LCD_TEXT_RED;
    st.level = CSI_LCD_ERROR;
    snprintf(st.state, sizeof(st.state), "SD RECOVERING");
  } else if (!sd.mounted) {
    snprintf(st.lines[5], sizeof(st.lines[5]), "SD none");
  } else if (sd.recording) {
    const uint32_t sec = (millis() - sd.started_ms) / 1000;
    snprintf(st.lines_right[4], sizeof(st.lines_right[4]), "%02lu:%02lu:%02lu",
             (unsigned long)(sec / SEC_PER_HOUR), (unsigned long)(sec % SEC_PER_HOUR / SEC_PER_MIN),
             (unsigned long)(sec % SEC_PER_MIN));
    st.line_right_fg[4] = CSI_LCD_TEXT_RED;
    st.line_fg[5] = CSI_LCD_TEXT_RED;
    if (sd.first_error[0] != '\0') {
      // カードに書けていない。REC のままだと気づけない（2026-10-07 の夜）ので、帯も赤にする
      snprintf(st.lines[5], sizeof(st.lines[5]), "SD ERR %.12s", sd.first_error);
      st.level = CSI_LCD_ERROR;
      snprintf(st.state, sizeof(st.state), "SD ERROR");
    } else if (sd.dropped_lines > 0) {
      snprintf(st.lines[5], sizeof(st.lines[5]), "REC %.5s DROP %lu", sd.name,
               (unsigned long)sd.dropped_lines);
    } else {
      // 今のファイル・その量・セッション全体の量（2026-10-09: ファイルを 1 時間ごとに切り替えるため）。
      // 赤字と経過時間で記録中と分かるので REC は省く（Font4 で最長 279 px・幅は 304 px）
      char file_size[12];
      char total_size[12];
      lcd_format_size(file_size, sizeof(file_size), sd.bytes);
      lcd_format_size(total_size, sizeof(total_size), sd.total_bytes);
      snprintf(st.lines[5], sizeof(st.lines[5]), "%.5s %s all %s", sd.name, file_size, total_size);
    }
  } else {
    snprintf(st.lines[5], sizeof(st.lines[5]), "SD idle %.1fGB", sd.free_bytes / 1073741824.0);
  }
  // 時刻は状態の帯の右端に常に出す（記録中は 6 行目が量と経過で埋まり、見えなくなっていた・2026-10-08）
  if (g_time_known) {
    const time_t local = static_cast<time_t>(
        (g_unix_ms_at_boot + static_cast<int64_t>(millis())) / 1000 + g_tz_sec);
    struct tm tmv;
    gmtime_r(&local, &tmv);
    strftime(st.state_right, sizeof(st.state_right), "%H:%M", &tmv);
  } else {
    snprintf(st.state_right, sizeof(st.state_right), "--:--");
  }
#else
  csi_lcd_format_uptime(st.lines[5], sizeof(st.lines[5]), millis());
#endif
  g_lcd_last = st;
  g_lcd_have = true;
  apply_mark_overlay(st);
  csi_lcd_post(st);
}
#endif

// ---- 状況報告と命令の受け付け -------------------------------------------------

void loop() {
  static uint32_t s_last_matched = 0;
  static uint32_t s_last_ms = millis();

  delay(COMMAND_POLL_MS);
  poll_commands();
#if CSI_M5_LCD
  speaker_update();
#endif
#if CSI_SD
  poll_buttons();
  sd_poll_recover();
  sd_poll_rotate();
#endif
  service_pico();
  send_keepalive();
  check_watchdog();
  update_led();
  const uint32_t now_ms = millis();
  if (now_ms - s_last_ms < REPORT_INTERVAL_MS) return;
  const uint32_t matched = g_matched;
  const float rate_hz = (matched - s_last_matched) * 1000.0f / (now_ms - s_last_ms);
  // temp_c = 自分のチップの温度、peer_temp_c = 送信機のチップの温度（版 4 から。届いていなければ nan）。
  // どちらも ESP32 の内蔵センサーの目安で、室温ではない（2026-10-10: 失敗の多い晩と少ない晩を比べるため）
  g_temp_c = temperatureRead();
  char peer_temp[8] = "nan";
  if (g_peer_temp_known) snprintf(peer_temp, sizeof(peer_temp), "%d", (int)g_peer_temp_c);
#if CSI_M5_LCD
  poll_power();
#endif
  // '#' で始まる行は CSV の本体ではない（取得側で読み飛ばす）
  note_printf("# stats rate_hz=%.1f matched=%lu other=%lu dropped=%lu truncated=%lu written=%lu "
                "frames=%lu parse_fail=%lu seq_unmatched=%lu seq_dt_max_us=%ld channel=%u "
                "relay_ok=%lu relay_err=%lu matched2=%lu frames2=%lu pico=%d pico_join_ok=%lu "
                "pico_join_err=%lu ctrl_bad=%lu peer_tx_power_dbm=%.2f tx_restarts=%lu "
                "heap_free=%u temp_c=%.1f peer_temp_c=%s\n",
                rate_hz, (unsigned long)matched, (unsigned long)g_other,
                (unsigned long)g_dropped, (unsigned long)g_truncated, (unsigned long)g_written,
                (unsigned long)g_frames, (unsigned long)g_parse_fail,
                (unsigned long)g_seq_unmatched, (long)g_seq_dt_max_us, g_channel,
                (unsigned long)g_relay_ok, (unsigned long)g_relay_err,
                (unsigned long)g_matched2, (unsigned long)g_frames2, pico_connected() ? 1 : 0,
                (unsigned long)g_pico_join_ok, (unsigned long)g_pico_join_err,
                (unsigned long)g_ctrl_bad, g_peer_qdbm_known ? g_peer_qdbm / QDBM_PER_DBM : -1.0f,
                (unsigned long)g_tx_restarts, (unsigned)ESP.getFreeHeap(), g_temp_c, peer_temp);
#if CSI_SD
  if (sd_log::recording()) {
    const sd_log::Status st = sd_log::status();
    // synced = 確定の後にカード上で読んだ長さ。bytes と合わなければ sync_lag が増える（2026-10-08）。
    // first_error_s = セッションの開始から最初の失敗までの秒（-1 = なし）
    const long first_s = st.first_error_ms != 0
                             ? static_cast<long>((st.first_error_ms - st.started_ms) / 1000)
                             : -1L;
    // disk_* = ドライバの書き込み（回数・失敗・1 回の最大・最後の失敗の時間とカードの応答）、
    // disk_retry = 書き直した回数・disk_retry_ok = 書き直しで救えた回数（2026-10-10）、
    // status_* = カードへの状態の問い合わせ（失敗・最大・最後の失敗の時間）、read_fail = 読み出しの失敗
    note_printf("# sdstat name=%s bytes=%llu total_bytes=%llu part=%lu rotate_max_ms=%lu "
               "synced=%llu sync_lag=%lu dropped_lines=%lu "
               "buffer_high=%lu write_max_ms=%lu flush_max_ms=%lu errors=%lu last_error=%s "
               "errno=%d first_error=%s first_errno=%d first_error_s=%ld disk_writes=%lu "
               "disk_fail=%lu disk_retry=%lu disk_retry_ok=%lu disk_max_ms=%lu disk_fail_ms=%lu "
               "disk_fail_card_ok=%d status_fail=%lu status_max_ms=%lu status_fail_ms=%lu read_fail=%lu\n",
               st.name, (unsigned long long)st.bytes, (unsigned long long)st.total_bytes,
               (unsigned long)st.part, (unsigned long)st.rotate_max_ms,
               (unsigned long long)st.synced_bytes,
               (unsigned long)st.sync_lag, (unsigned long)st.dropped_lines,
               (unsigned long)st.buffer_high, (unsigned long)st.write_max_ms,
               (unsigned long)st.flush_max_ms, (unsigned long)st.errors,
               st.last_error[0] ? st.last_error : "-", st.last_errno,
               st.first_error[0] ? st.first_error : "-", st.first_errno, first_s,
               (unsigned long)st.disk_writes, (unsigned long)st.disk_fail,
               (unsigned long)st.disk_retry, (unsigned long)st.disk_retry_ok,
               (unsigned long)st.disk_write_max_ms, (unsigned long)st.disk_fail_ms,
               (int)st.disk_fail_card_ok, (unsigned long)st.status_fail,
               (unsigned long)st.status_max_ms, (unsigned long)st.status_fail_ms,
               (unsigned long)st.read_fail);
  }
#endif
#if CSI_M5_LCD
  post_lcd(rate_hz);
#endif
  s_last_matched = matched;
  s_last_ms = now_ms;
}
