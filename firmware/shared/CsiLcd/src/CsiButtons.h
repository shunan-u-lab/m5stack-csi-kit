// M5Stack Basic の 3 つのボタンと画面の明るさ（送信機・受信機で共通）。
//
// - ボタン: A = GPIO39（左）、B = GPIO38（中）、C = GPIO37（右）。押すと LOW（基板に引き上げ抵抗がある）
// - C を LONG_PRESS_MS 押し続けると、画面の明るさが 普通 → 暗い → 消灯 → 普通 と変わる
//   （csi_lcd_cycle_brightness。NVS に覚えるので再起動しても同じ明るさ。夜間は消灯のままにできる）
// - 消灯中は、どのボタンも最初の 1 回は画面を点けるだけ（暗い中で押し間違えて記録を止めないように）
// - poll は loop から 10 ms 程度ごとに呼ぶ。1 回の呼び出しで返す動作は 1 つ
#pragma once

#include <Arduino.h>

#include "CsiLcd.h"

namespace csi_buttons {

constexpr uint8_t PIN_A = 39;
constexpr uint8_t PIN_B = 38;
constexpr uint8_t PIN_C = 37;
constexpr uint32_t DEBOUNCE_MS = 30;     // 押した・離したと決めるまで同じ状態が続く時間 [ms]
constexpr uint32_t LONG_PRESS_MS = 1000;  // C の長押し [ms]
constexpr size_t N_BUTTONS = 3;

// poll が返す動作
enum class Action : uint8_t {
  NONE,
  A,           // A を押した
  B,           // B を押した
  C_SHORT,     // C を短く押して離した
  BRIGHTNESS,  // C の長押しで明るさを変えた（csi_lcd_brightness で今の値）
  WAKE,        // 消灯中に押したので画面を点けた（押したボタンの動作はしない）
};

class Buttons {
 public:
  void begin() {
    for (State &s : states_) pinMode(s.pin, INPUT);
  }

  Action poll() {
    const uint32_t now = millis();
    for (size_t i = 0; i < N_BUTTONS; i++) {
      State &s = states_[i];
      const bool raw = digitalRead(s.pin) == LOW;
      if (raw != s.last_raw) {
        s.last_raw = raw;
        s.since_ms = now;
        continue;
      }
      if (raw != s.pressed && now - s.since_ms >= DEBOUNCE_MS) {
        s.pressed = raw;
        if (raw) {  // 押した
          s.pressed_at_ms = now;
          s.long_done = false;
          s.swallowed = csi_lcd_screen_off();
          if (s.swallowed) {
            csi_lcd_wake();
            return Action::WAKE;
          }
          if (i == 0) return Action::A;
          if (i == 1) return Action::B;
        } else if (i == 2 && !s.swallowed && !s.long_done) {  // C を短く押して離した
          return Action::C_SHORT;
        }
        continue;
      }
      if (i == 2 && s.pressed && !s.swallowed && !s.long_done &&
          now - s.pressed_at_ms >= LONG_PRESS_MS) {
        s.long_done = true;  // 押している間に変える（離すのを待たない）
        csi_lcd_cycle_brightness();
        return Action::BRIGHTNESS;
      }
    }
    return Action::NONE;
  }

 private:
  struct State {
    uint8_t pin;
    bool pressed;
    bool last_raw;
    uint32_t since_ms;
    uint32_t pressed_at_ms;  // 押した時刻 [ms]
    bool long_done;          // この押し下げで長押しの動作をした
    bool swallowed;          // この押し下げは画面を点けるだけに使った
  };
  State states_[N_BUTTONS] = {{PIN_A, false, false, 0, 0, false, false},
                              {PIN_B, false, false, 0, 0, false, false},
                              {PIN_C, false, false, 0, 0, false, false}};
};

}  // namespace csi_buttons
