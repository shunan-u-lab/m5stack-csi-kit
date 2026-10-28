// 送信機・受信機の起動と見張り（2026-10-07）。
//
// - 前回なぜ再起動したかを文字にする（起動時の表示の boot=。ウォッチドッグが働いたかを後から確かめる）
// - ループのウォッチドッグ: Arduino-ESP32 2.0.17 の設定は CONFIG_ESP_TASK_WDT_TIMEOUT_S=5・
//   CONFIG_ESP_TASK_WDT_PANIC=y（5 秒応答がなければパニックとして再起動）。setup の最後に
//   csi_boot::start_loop_watchdog() を呼ぶと、loop を回るたびに Arduino の loopTask が知らせる。
//   loop の中で長く待つ所は csi_boot::wait_ms() を使う（待つ間も知らせる）
#pragma once

#include <Arduino.h>
#include <esp_system.h>
#include <esp_task_wdt.h>

namespace csi_boot {

constexpr uint32_t WAIT_STEP_MS = 1;  // wait_ms で知らせる間隔 [ms]

inline bool &watchdog_on() {
  static bool on = false;
  return on;
}

// 前回の再起動の理由（起動時の表示用の短い英字）。
inline const char *reset_reason_name() {
  switch (esp_reset_reason()) {
    case ESP_RST_POWERON: return "poweron";
    case ESP_RST_EXT: return "ext";
    case ESP_RST_SW: return "sw";
    case ESP_RST_PANIC: return "panic";
    case ESP_RST_INT_WDT: return "int_wdt";
    case ESP_RST_TASK_WDT: return "task_wdt";
    case ESP_RST_WDT: return "wdt";
    case ESP_RST_DEEPSLEEP: return "deepsleep";
    case ESP_RST_BROWNOUT: return "brownout";
    case ESP_RST_SDIO: return "sdio";
    default: return "other";
  }
}

// loop のウォッチドッグを始める（setup の最後に 1 回）。
inline void start_loop_watchdog() {
  enableLoopWDT();
  watchdog_on() = true;
}

// ウォッチドッグに知らせながら ms 待つ（loop の中の長い待ち用）。
inline void wait_ms(uint32_t ms) {
  const uint32_t t0 = millis();
  while (millis() - t0 < ms) {
    delay(WAIT_STEP_MS);
    if (watchdog_on()) esp_task_wdt_reset();
  }
}

}  // namespace csi_boot
