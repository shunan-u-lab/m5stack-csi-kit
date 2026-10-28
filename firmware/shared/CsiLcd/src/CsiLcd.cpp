#include "CsiLcd.h"

#include <Arduino.h>
#include <M5GFX.h>
#include <Preferences.h>
#include <freertos/queue.h>
#include <freertos/semphr.h>
#include <stdio.h>
#include <string.h>

namespace {

// ---- 画面の配置 [px]（M5Stack Basic は 320 × 240） ----------------------------
constexpr int32_t HEADER_H = 40;        // 機器番号と役割の帯
constexpr int32_t STATE_Y = HEADER_H;   // 状態の帯
constexpr int32_t STATE_H = 44;
constexpr int32_t BODY_Y = STATE_Y + STATE_H + 6;
constexpr int32_t LINE_H = 25;          // 本文 1 行の高さ（Font4 は 26 px。詰めて 6 行入れる）
constexpr int32_t MARGIN_X = 8;
// 明るさの段（0〜255）: 普通・暗い・消灯。明るすぎると熱と消費電流が増える
constexpr uint8_t BRIGHT_LEVELS[] = {96, 12, 0};
constexpr size_t N_BRIGHT = sizeof(BRIGHT_LEVELS) / sizeof(BRIGHT_LEVELS[0]);
constexpr const char *PREFS_NS = "csi_lcd";
constexpr const char *PREF_BRIGHT = "bright";

// ---- 色（RGB565） -------------------------------------------------------------
constexpr uint16_t COLOR_BG = TFT_BLACK;
constexpr uint16_t COLOR_TEXT = TFT_WHITE;
constexpr uint16_t COLOR_HEADER = 0x10A2;  // 濃い灰色
constexpr uint16_t LEVEL_BG[] = {0x0400 /* 緑 */, 0xFD20 /* 橙寄りの黄 */, 0xB000 /* 赤 */};
constexpr uint16_t LEVEL_FG[] = {TFT_WHITE, TFT_BLACK, TFT_WHITE};

// ---- 描画タスク ---------------------------------------------------------------
constexpr uint32_t TASK_STACK = 4096;   // [byte]
constexpr UBaseType_t TASK_PRIORITY = 1;
constexpr BaseType_t TASK_CORE = 0;     // Arduino の loop（コア 1）から離す

M5GFX g_display;
QueueHandle_t g_queue = nullptr;
SemaphoreHandle_t g_bus_lock = nullptr;  // SD と共有する SPI バスの鍵（無ければ使わない）
char g_label[8] = "";
char g_role[16] = "";
char g_version[CSI_LCD_VERSION_LEN + 1] = "";
uint8_t g_bright_index = 0;
bool g_display_ready = false;
constexpr uint16_t COLOR_VERSION = 0xBDF7;  // 明るい灰色（版は控えめに）

// 背景色つきで書き、行の残りを塗りつぶす（前の文字が長くても残らない。全体を消さないのでちらつかない）
void draw_padded(const char *text, int32_t x, int32_t y, int32_t h, uint16_t fg, uint16_t bg,
                 const lgfx::IFont *font) {
  g_display.setFont(font);
  g_display.setTextColor(fg, bg);
  g_display.setTextDatum(textdatum_t::middle_left);
  const int32_t w = g_display.drawString(text, x, y + h / 2);
  g_display.fillRect(x + w, y, g_display.width() - x - w, h, bg);
}

void draw_header() {
  g_display.fillRect(0, 0, g_display.width(), HEADER_H, COLOR_HEADER);
  g_display.setFont(&fonts::Font4);
  g_display.setTextColor(COLOR_TEXT, COLOR_HEADER);
  g_display.setTextDatum(textdatum_t::middle_left);
  g_display.setTextSize(1.25f);
  g_display.drawString(g_label, MARGIN_X, HEADER_H / 2);
  g_display.setTextSize(1.0f);
  g_display.setTextDatum(textdatum_t::middle_right);
  if (g_version[0] == '\0') {
    g_display.drawString(g_role, g_display.width() - MARGIN_X, HEADER_H / 2);
    return;
  }
  // 右側を上下 2 段に: 上 = 役割、下 = 版（Font2 は 16 px）
  g_display.setFont(&fonts::Font2);
  g_display.drawString(g_role, g_display.width() - MARGIN_X, HEADER_H / 4 + 1);
  g_display.setTextColor(COLOR_VERSION, COLOR_HEADER);
  g_display.drawString(g_version, g_display.width() - MARGIN_X, HEADER_H * 3 / 4);
}

void draw_status(const CsiLcdStatus &s) {
  const uint8_t level = s.level <= CSI_LCD_ERROR ? s.level : CSI_LCD_ERROR;
  // 文字の背景は文字の高さ（Font4 は 26 px）しか塗らないので、色が変わったら帯（44 px）全体を塗り直す
  // （2026-10-07: 黄 → 緑で、文字の上下に前の色が細く残っていた）
  static int s_last_level = -1;
  if (s_last_level != level) {
    g_display.fillRect(0, STATE_Y, g_display.width(), STATE_H, LEVEL_BG[level]);
    s_last_level = level;
  }
  g_display.fillRect(0, STATE_Y, MARGIN_X, STATE_H, LEVEL_BG[level]);
  draw_padded(s.state, MARGIN_X, STATE_Y, STATE_H, LEVEL_FG[level], LEVEL_BG[level],
              &fonts::Font4);
  // 右端（時刻など）: draw_padded が行の残りを塗った後に右寄せで重ねる（2026-10-08）
  if (s.state_right[0] != '\0') {
    g_display.setTextDatum(textdatum_t::middle_right);
    g_display.drawString(s.state_right, g_display.width() - MARGIN_X, STATE_Y + STATE_H / 2);
  }
  for (size_t i = 0; i < CSI_LCD_LINES; i++) {
    const int32_t y = BODY_Y + static_cast<int32_t>(i) * LINE_H;
    const uint16_t fg = s.line_fg[i] != 0 ? s.line_fg[i] : COLOR_TEXT;
    draw_padded(s.lines[i], MARGIN_X, y, LINE_H, fg, COLOR_BG, &fonts::Font4);
    if (s.lines_right[i][0] != '\0') {  // 右端（2026-10-08: 記録の経過）
      g_display.setTextColor(s.line_right_fg[i] != 0 ? s.line_right_fg[i] : COLOR_TEXT, COLOR_BG);
      g_display.setTextDatum(textdatum_t::middle_right);
      g_display.drawString(s.lines_right[i], g_display.width() - MARGIN_X, y + LINE_H / 2);
    }
  }
}

void lcd_task(void *arg) {
  (void)arg;
  CsiLcdStatus s;
  for (;;) {
    if (xQueueReceive(g_queue, &s, portMAX_DELAY) != pdTRUE) continue;
    if (g_bus_lock != nullptr) xSemaphoreTake(g_bus_lock, portMAX_DELAY);
    g_display.startWrite();
    draw_status(s);
    g_display.endWrite();
    if (g_bus_lock != nullptr) xSemaphoreGive(g_bus_lock);
  }
}

// 文字列を必ず終端して写す
void copy_text(char *dst, size_t dst_len, const char *src) {
  if (dst_len == 0) return;
  strncpy(dst, src != nullptr ? src : "", dst_len - 1);
  dst[dst_len - 1] = '\0';
}

}  // namespace

void csi_lcd_begin(const char *label, const char *role, const char *version) {
  copy_text(g_label, sizeof(g_label), label);
  copy_text(g_role, sizeof(g_role), role);
  copy_text(g_version, sizeof(g_version), version);
  if (!g_display.init()) {
    Serial.printf("# lcd init_failed (display disabled)\n");
    return;
  }
  {
    Preferences p;
    if (p.begin(PREFS_NS, true)) {  // まだ何も書いていなければ開けない（普通の段）
      g_bright_index = p.getUChar(PREF_BRIGHT, 0);
      p.end();
    }
    if (g_bright_index >= N_BRIGHT) g_bright_index = 0;
  }
  g_display.setBrightness(BRIGHT_LEVELS[g_bright_index]);
  g_display_ready = true;
  g_display.fillScreen(COLOR_BG);
  draw_header();
  g_queue = xQueueCreate(1, sizeof(CsiLcdStatus));
  if (g_queue == nullptr) {
    Serial.printf("# lcd queue_failed (display disabled)\n");
    return;
  }
  if (xTaskCreatePinnedToCore(lcd_task, "lcd", TASK_STACK, nullptr, TASK_PRIORITY, nullptr,
                              TASK_CORE) != pdPASS) {
    Serial.printf("# lcd task_failed (display disabled)\n");
    vQueueDelete(g_queue);
    g_queue = nullptr;
  }
}

void csi_lcd_post(const CsiLcdStatus &status) {
  if (g_queue == nullptr) return;
  xQueueOverwrite(g_queue, &status);
}

namespace {
void set_bright_index(uint8_t index) {
  g_bright_index = index < N_BRIGHT ? index : 0;
  if (g_display_ready) g_display.setBrightness(BRIGHT_LEVELS[g_bright_index]);  // 背光の PWM だけ
  Preferences p;
  if (p.begin(PREFS_NS, false)) {
    p.putUChar(PREF_BRIGHT, g_bright_index);
    p.end();
  }
}
}  // namespace

void csi_lcd_cycle_brightness() { set_bright_index(static_cast<uint8_t>((g_bright_index + 1) % N_BRIGHT)); }

void csi_lcd_wake() {
  if (csi_lcd_screen_off()) set_bright_index(0);
}

bool csi_lcd_screen_off() { return BRIGHT_LEVELS[g_bright_index] == 0; }

uint8_t csi_lcd_brightness() { return BRIGHT_LEVELS[g_bright_index]; }

void csi_lcd_set_bus_lock(void *mutex) { g_bus_lock = static_cast<SemaphoreHandle_t>(mutex); }

void csi_lcd_format_uptime(char *out, size_t out_len, uint32_t ms) {
  const uint32_t sec = ms / 1000;
  snprintf(out, out_len, "up %02lu:%02lu:%02lu", (unsigned long)(sec / 3600),
           (unsigned long)(sec / 60 % 60), (unsigned long)(sec % 60));
}
