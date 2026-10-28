// M5Stack の液晶に稼働状況を出す（csi-tx・csi-rx の M5Stack 用 env だけで使う）。
//
// - 描画は別タスク（コア 0・低い優先度）で行う。送受信の処理（100 Hz）は csi_lcd_post で
//   内容をキューに置くだけで、描画を待たない。キューは 1 件で、新しい内容が古いものを上書きする
// - 文字は ASCII だけ（日本語フォントはプログラム領域を大きく使うため）
// - 画面: 上の帯 = 機器番号（左）と役割・ファームウェアの版（右の上下 2 段）、次の帯 = 状態
//   （色で正常・注意・異常。右端に時刻などを出せる）、その下に最大 CSI_LCD_LINES 行
#pragma once

#include <stddef.h>
#include <stdint.h>

// 本文の文字の色（RGB565。CsiLcdStatus::line_fg に入れる。0 は既定の白）
constexpr uint16_t CSI_LCD_TEXT_RED = 0xF800;  // 記録中など（2026-10-08）

// 状態の帯の色
enum CsiLcdLevel : uint8_t {
  CSI_LCD_OK = 0,     // 緑: 正常
  CSI_LCD_WARN = 1,   // 黄: 注意（受信が少ない・相手と通じていない 等）
  CSI_LCD_ERROR = 2,  // 赤: 異常（受信なし・送信エラー 等）
};

constexpr size_t CSI_LCD_LINES = 6;       // 本文の行数
constexpr size_t CSI_LCD_LINE_LEN = 28;   // 1 行の最大文字数（終端を除く）
constexpr size_t CSI_LCD_STATE_LEN = 20;  // 状態の帯の最大文字数（終端を除く）
constexpr size_t CSI_LCD_STATE_RIGHT_LEN = 8;  // 状態の帯の右端の最大文字数（終端を除く。例: 時刻）

struct CsiLcdStatus {
  CsiLcdLevel level;
  char state[CSI_LCD_STATE_LEN + 1];
  char state_right[CSI_LCD_STATE_RIGHT_LEN + 1];  // 帯の右端に右寄せで出す（空なら出さない）
  char lines[CSI_LCD_LINES][CSI_LCD_LINE_LEN + 1];
  uint16_t line_fg[CSI_LCD_LINES];  // 行ごとの文字の色（RGB565。0 = 既定の白）
  // 行の右端に右寄せで出す文字（空なら出さない。左の文字と重なれば右が上に描かれる）とその色
  char lines_right[CSI_LCD_LINES][CSI_LCD_STATE_RIGHT_LEN + 1];
  uint16_t line_right_fg[CSI_LCD_LINES];
};

constexpr size_t CSI_LCD_VERSION_LEN = 24;  // 版の最大文字数（終端を除く）

// 液晶を初期化して描画タスクを起こす。label は機器番号（例: "T1"）、role は役割（例: "TRANSMITTER"）、
// version はファームウェアの版（例: "1007-db82545"。nullptr・空なら出さない）。
// 失敗しても送受信は止めない（理由をシリアルに出して表示だけを諦める）。
void csi_lcd_begin(const char *label, const char *role, const char *version = nullptr);

// 表示する内容を渡す（待たない）。csi_lcd_begin の前・失敗後に呼んでも何もしない。
void csi_lcd_post(const CsiLcdStatus &status);

// SPI バスの鍵を渡す（SD カードと液晶が同じバスを使う M5Stack Basic 用・2026-10-07）。
// 渡すと、描画の間だけ鍵を取る（SD の書き込みと重ならない）。nullptr なら鍵を使わない。
void csi_lcd_set_bus_lock(void *mutex);

// 画面の明るさ（2026-10-07）: 段は 普通・暗い・消灯。NVS に覚え、csi_lcd_begin で前回の段から始める。
// 操作はボタン C の長押し（CsiButtons.h）。
void csi_lcd_cycle_brightness();  // 次の段へ（覚える）
void csi_lcd_wake();              // 消灯なら普通の段へ（覚える）
bool csi_lcd_screen_off();        // 今が消灯の段か
uint8_t csi_lcd_brightness();     // 今の明るさ（0〜255）

// 起動からの時間を "up 01:23:45" の形で書く（本文の行に使う）。
void csi_lcd_format_uptime(char *out, size_t out_len, uint32_t ms);
