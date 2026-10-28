// 送信機と受信機の組（相手の MAC と制御の鍵）を NVS に置く（2026-10-27・docs/decisions/0002）。
//
// 1 種類のビルド済み .bin を配るため、相手の MAC と鍵はビルド時に埋め込まず、書き込み後に
// シリアルの命令で NVS に置く（PC 側は csikit flash が送る）:
//   PAIRSET <相手の MAC 16 進 12 文字> <鍵 16 進 32 文字>   置いて再起動する
//   PAIRCLEAR                                                消して再起動する（機材を返す・人に渡す前）
//
// - 鍵の値は応答・液晶・ログに出さない（設定済みかどうかだけ出す）
// - 未設定・読めない・形が違うときは「組なし」として扱う。呼ぶ側は無線の命令を受け付けない（失敗時は安全側）
// - 名前空間は受信機の他の設定（"csi"・"csi_sdev"）と分ける。PAIRCLEAR で他の設定を巻き込まない
#pragma once

#include <Arduino.h>
#include <Preferences.h>
#include <string.h>

#include <CsiAuth.h>

namespace csi_pair {

constexpr size_t MAC_LEN = 6;                        // [byte]
constexpr const char *NVS_NS = "csi_pair";           // NVS の名前空間
constexpr const char *KEY_PEER = "peer";             // 相手の MAC（6 バイト）
constexpr const char *KEY_AUTH = "key";              // 鍵（csi_auth::KEY_LEN バイト）
constexpr uint32_t REBOOT_DELAY_MS = 50;             // 応答を出し切ってから再起動するまで [ms]

struct Pair {
  uint8_t peer[MAC_LEN] = {};
  uint8_t key[csi_auth::KEY_LEN] = {};
  bool ok = false;  // 両方そろって読めた
};

// 全部 0 の MAC（未設定の印として使えないよう、PAIRSET で拒む）。
inline bool is_zero_mac(const uint8_t mac[MAC_LEN]) {
  for (size_t i = 0; i < MAC_LEN; i++) {
    if (mac[i] != 0) return false;
  }
  return true;
}

// 16 進 12 文字（区切りなし）を MAC にする。形が違えば false。
inline bool parse_mac(const char *hex, uint8_t out[MAC_LEN]) {
  if (hex == nullptr || strlen(hex) != MAC_LEN * 2) return false;
  for (size_t i = 0; i < MAC_LEN; i++) {
    const int hi = csi_auth::hex_value(hex[2 * i]);
    const int lo = csi_auth::hex_value(hex[2 * i + 1]);
    if (hi < 0 || lo < 0) return false;
    out[i] = static_cast<uint8_t>(hi << 4 | lo);
  }
  return true;
}

// NVS から読む。未設定・長さが違うときは ok=false（peer・key は 0 のまま）。
inline Pair load() {
  Pair pair;
  Preferences p;
  if (!p.begin(NVS_NS, true)) return pair;  // 一度も書いていなければ開けない
  const bool peer_ok = p.getBytesLength(KEY_PEER) == MAC_LEN &&
                       p.getBytes(KEY_PEER, pair.peer, MAC_LEN) == MAC_LEN;
  const bool key_ok = p.getBytesLength(KEY_AUTH) == csi_auth::KEY_LEN &&
                      p.getBytes(KEY_AUTH, pair.key, csi_auth::KEY_LEN) == csi_auth::KEY_LEN;
  p.end();
  pair.ok = peer_ok && key_ok && !is_zero_mac(pair.peer);
  if (!pair.ok) pair = Pair();
  return pair;
}

// PAIRSET の引数を確かめて NVS に置く。成功なら nullptr、失敗なら理由。
inline const char *store(char *arg) {
  char *save = nullptr;
  const char *mac_hex = strtok_r(arg, " ", &save);
  const char *key_hex = strtok_r(nullptr, " ", &save);
  const char *extra = strtok_r(nullptr, " ", &save);
  uint8_t mac[MAC_LEN] = {};
  uint8_t key[csi_auth::KEY_LEN] = {};
  if (extra != nullptr || !parse_mac(mac_hex, mac) || is_zero_mac(mac)) return "bad_mac";
  if (!csi_auth::parse_key(key_hex, key)) return "bad_key";
  Preferences p;
  if (!p.begin(NVS_NS, false)) {
    memset(key, 0, sizeof(key));
    return "nvs_open";
  }
  const bool ok = p.putBytes(KEY_PEER, mac, MAC_LEN) == MAC_LEN &&
                  p.putBytes(KEY_AUTH, key, csi_auth::KEY_LEN) == csi_auth::KEY_LEN;
  p.end();
  memset(key, 0, sizeof(key));
  return ok ? nullptr : "nvs_write";
}

// PAIRCLEAR: 名前空間ごと消す。成功なら nullptr、失敗なら理由。
inline const char *clear() {
  Preferences p;
  if (!p.begin(NVS_NS, false)) return "nvs_open";
  const bool ok = p.clear();
  p.end();
  return ok ? nullptr : "nvs_clear";
}

// 応答を出し切ってから再起動する（設定は次の起動で読む）。
inline void restart() {
  Serial.flush();
  delay(REBOOT_DELAY_MS);
  ESP.restart();
}

}  // namespace csi_pair
