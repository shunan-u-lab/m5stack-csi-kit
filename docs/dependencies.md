# 使っている既存のもの

既存の機器・ソフト・サーバを使ったら、ここに 1 行足す。
相手は黙って変わる。版は後から思い出せないので、使い始めたその場で書く。

| 何を | 分野 | 版 | 誰の持ち物 | 資料（`reference/`） | 変わったらどう気づくか |
|---|---|---|---|---|---|
| M5Stack Basic（ESP32・USB は CH9102） | ファーム | 基板の版は利用者ごと | メーカー | （未収集） | `csikit doctor` が WHO の応答と USB の VID/PID を表示する |
| PlatformIO Core | ファーム | 6.2.0（`uv tool`） | 外部（PlatformIO） | — | CI のビルドが落ちる |
| espressif32（Arduino-ESP32 2.0.17 / ESP-IDF v4.4.7） | ファーム | 6.9.0（`platformio.ini` で固定） | 外部（PlatformIO・Espressif） | — | 固定しているので変わらない。上げるときは実機で受信レートを測り直す |
| M5GFX | ファーム | 0.1.17（`lib_deps` で固定） | メーカー | — | 同上 |
| esptool | PC | 5.3.1（`uv tool`） | 外部（Espressif） | — | `csikit doctor` が版を表示する |
| uv | PC | 0.11.2 で確認 | 外部（Astral） | — | `uv.lock` で Python の依存を固定 |
| Python ライブラリ（numpy・pandas・matplotlib・pyserial ほか） | PC | `uv.lock` のとおり（2026-10-27 時点 numpy 2.5.4・pandas 3.1.0・matplotlib 3.11.2・pyserial 3.5、Python 3.14.3） | 外部 | — | CI（3 OS）のテストが落ちる |

- 「誰の持ち物」には、持ち主の立場（自分・利用者・メーカー・外部サービス）を書く。実名・連絡先は書かない
- 「変わったらどう気づくか」が書けないものは、突き合わせのテストを足すか、確かめる手順を書く
