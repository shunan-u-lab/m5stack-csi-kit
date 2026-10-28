// 送信機（firmware/csi-tx）と受信機（firmware/csi-rx）の制御パケットの認証（2026-10-07）。
//
// ESP-NOW のブロードキャストは暗号化できない（ESP-IDF の ESP-NOW の説明）。制御パケット
// （チャンネル切り替え・合図・了解）は、送り元の MAC を偽れば誰でも作れたため、共有の鍵で署名する。
//
// - 署名: HMAC-SHA256（mbedtls）の先頭 TAG_LEN バイト。鍵は KEY_LEN バイト
// - 鍵はリポジトリにも .bin にも入れない。組ごとに PC が乱数で作り、書き込み後にシリアルの PAIRSET で
//   両方の NVS に置く（firmware/shared/CsiPair・docs/decisions/0002）。組が無い・形の違う鍵は「鍵なし」扱いにし、
//   無線の命令を一切受け付けない（失敗時は安全側）
// - 録音した命令の流し直しを防ぐため、送信機はデータのパケットに合言葉（nonce・起動ごとの乱数）を載せ、
//   受信機はその合言葉を含めて署名する。送信機は命令を受け入れるたびに合言葉を変える
//
// 署名は傍受を防がない（中身は平文）。送っている電波から第三者が CSI を測ることも防げない。
#pragma once

#include <Arduino.h>
#include <mbedtls/md.h>
#include <string.h>

namespace csi_auth {

constexpr size_t KEY_LEN = 16;   // 鍵の長さ [byte]（16 進 32 文字）
constexpr size_t TAG_LEN = 8;    // 署名の長さ [byte]（HMAC-SHA256 の先頭）
constexpr size_t SHA256_LEN = 32;

// 16 進 1 文字を値にする（違えば -1）。
inline int hex_value(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  return -1;
}

// 16 進 32 文字を鍵にする。形が違えば false（out は使わない）。
inline bool parse_key(const char *hex, uint8_t out[KEY_LEN]) {
  if (hex == nullptr || strlen(hex) != KEY_LEN * 2) return false;
  for (size_t i = 0; i < KEY_LEN; i++) {
    const int hi = hex_value(hex[2 * i]);
    const int lo = hex_value(hex[2 * i + 1]);
    if (hi < 0 || lo < 0) return false;
    out[i] = static_cast<uint8_t>(hi << 4 | lo);
  }
  return true;
}

// msg の署名を out に書く。失敗（mbedtls の誤り）なら false。
inline bool sign(const uint8_t key[KEY_LEN], const uint8_t *msg, size_t len,
                 uint8_t out[TAG_LEN]) {
  uint8_t full[SHA256_LEN];
  const mbedtls_md_info_t *info = mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);
  if (info == nullptr || mbedtls_md_hmac(info, key, KEY_LEN, msg, len, full) != 0) return false;
  memcpy(out, full, TAG_LEN);
  return true;
}

// 署名が合うか（比べる時間が中身で変わらないように全バイトを比べる）。
inline bool verify(const uint8_t key[KEY_LEN], const uint8_t *msg, size_t len,
                   const uint8_t tag[TAG_LEN]) {
  uint8_t want[TAG_LEN];
  if (!sign(key, msg, len, want)) return false;
  uint8_t diff = 0;
  for (size_t i = 0; i < TAG_LEN; i++) diff |= want[i] ^ tag[i];
  return diff == 0;
}

}  // namespace csi_auth
