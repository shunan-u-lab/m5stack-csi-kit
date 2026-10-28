// M5Stack Basic の電源 IC（IP5306・I2C 0x75）の状態を読む（2026-10-07・充電できない原因の切り分け用）。
//
// - I2C は内部のバス（SDA 21・SCL 22。Grove 端子と同じ）。液晶・SD の SPI とは別
// - レジスタの意味は M5Stack の公式ライブラリ（M5Stack/src/utility/Power.cpp）の使い方に合わせた
//   （記憶にもとづく。IP5306 の公開資料は限られる）:
//     SYS_CTL0 0x00  bit5 昇圧の有効、bit4 充電の有効、bit2 負荷をつなぐと自動で起動、bit1 昇圧を常に出す、
//                    bit0 ボタンで電源を切れる
//     READ0    0x70  bit3 充電中（USB から充電している）
//     READ1    0x71  bit3 満充電
//     READ3    0x78  上位 4 bit が残量: 0x00 = 100%、0x80 = 75%、0xC0 = 50%、0xE0 = 25%、他 = 0%
// - 電池の電圧は読めない（IP5306 は出さない）。残量は 4 段階の目安
// - 注意（2026-10-07 実機）: 手元の受信機では、電池の入った底を外しても全レジスタが同じ値
//   （charging=1 full=0 level_pct=100）だった。この値は電池が効いている証拠にならない
//   （元の研究で切り分けを試みたが、打ち切った）
#pragma once

#include <Arduino.h>
#include <Wire.h>

namespace csi_power {

constexpr uint8_t I2C_ADDR = 0x75;
constexpr int PIN_SDA = 21;
constexpr int PIN_SCL = 22;
constexpr uint32_t I2C_HZ = 100000;  // [Hz]
constexpr uint8_t REG_SYS_CTL0 = 0x00;
constexpr uint8_t REG_SYS_CTL1 = 0x01;
constexpr uint8_t REG_SYS_CTL2 = 0x02;
constexpr uint8_t REG_READ0 = 0x70;
constexpr uint8_t REG_READ1 = 0x71;
constexpr uint8_t REG_READ2 = 0x72;
constexpr uint8_t REG_READ3 = 0x78;
constexpr uint8_t BIT_CHARGE_ENABLE = 1 << 4;  // SYS_CTL0
constexpr uint8_t BIT_BOOST_ENABLE = 1 << 5;   // SYS_CTL0
constexpr uint8_t BIT_CHARGING = 1 << 3;       // READ0
constexpr uint8_t BIT_FULL = 1 << 3;           // READ1
constexpr uint8_t LEVEL_MASK = 0xF0;           // READ3

struct Status {
  bool ok;           // I2C で読めた（IP5306 がいる）
  uint8_t ctl0, ctl1, ctl2, read0, read1, read2, read3;
  bool charge_enabled;
  bool boost_enabled;
  bool charging;
  bool full;
  int level_pct;     // 100 / 75 / 50 / 25 / 0（目安）
};

inline bool read_reg(uint8_t reg, uint8_t *out) {
  Wire.beginTransmission(I2C_ADDR);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom(static_cast<int>(I2C_ADDR), 1) != 1) return false;
  *out = static_cast<uint8_t>(Wire.read());
  return true;
}

inline int level_from(uint8_t read3) {
  switch (read3 & LEVEL_MASK) {
    case 0x00: return 100;
    case 0x80: return 75;
    case 0xC0: return 50;
    case 0xE0: return 25;
    default: return 0;
  }
}

inline Status read() {
  static bool s_begun = false;
  if (!s_begun) s_begun = Wire.begin(PIN_SDA, PIN_SCL, I2C_HZ);
  Status s = {};
  s.ok = s_begun && read_reg(REG_SYS_CTL0, &s.ctl0) && read_reg(REG_SYS_CTL1, &s.ctl1) &&
         read_reg(REG_SYS_CTL2, &s.ctl2) && read_reg(REG_READ0, &s.read0) &&
         read_reg(REG_READ1, &s.read1) && read_reg(REG_READ2, &s.read2) &&
         read_reg(REG_READ3, &s.read3);
  if (!s.ok) return s;
  s.charge_enabled = (s.ctl0 & BIT_CHARGE_ENABLE) != 0;
  s.boost_enabled = (s.ctl0 & BIT_BOOST_ENABLE) != 0;
  s.charging = (s.read0 & BIT_CHARGING) != 0;
  s.full = (s.read1 & BIT_FULL) != 0;
  s.level_pct = level_from(s.read3);
  return s;
}

// "<prefix> batt ok=1 charge_enabled=1 charging=1 full=0 level_pct=75 ctl0=0x.. ..." を out に書く（改行つき）。
inline void format(char *out, size_t n, const char *prefix) {
  const Status s = read();
  if (!s.ok) {
    snprintf(out, n, "%s batt ok=0 reason=no_ip5306_on_i2c\n", prefix);
    return;
  }
  snprintf(out, n,
           "%s batt ok=1 charge_enabled=%u boost_enabled=%u charging=%u full=%u level_pct=%d "
           "ctl0=0x%02x ctl1=0x%02x ctl2=0x%02x read0=0x%02x read1=0x%02x read2=0x%02x "
           "read3=0x%02x\n",
           prefix, s.charge_enabled, s.boost_enabled, s.charging, s.full, s.level_pct, s.ctl0,
           s.ctl1, s.ctl2, s.read0, s.read1, s.read2, s.read3);
}

}  // namespace csi_power
