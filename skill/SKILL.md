---
name: cmis-module-manager
description: 排查和驱动 CMIS Module Manager —— 浏览器里的光模块寄存器工具。用它的 REST API 直接问模块（别凭记忆猜寄存器），看懂界面上几处最容易误读的东西（闩锁标志的 ●! 历史、staged 与 active 配置不一致、置灰的控制、n/a 的标志位、Tx 偏置刻度倍数），判断某次 Apply 为什么没生效、某个读数为什么不对。触发词：CMIS, CMIS Module Manager, 光模块, 模块管理工具, QSFP-DD, OSFP, 寄存器, Page 11h, AppSel, DataPath, ConfigStatus, PRBS, 环回, loopback, 激光器调谐, 闩锁标志, latched flag, CH341, CH347, 光功率, 偏置电流。
---

# CMIS Module Manager

浏览器里的光模块管理工具，对着 OIF CMIS 5.4 写的。跑在
`http://127.0.0.1:5000`，通过 CH341 / CH347 / FTDI 的 USB-I2C 适配器读写模块。
MIT 开源、免费、可商用。

**先问模块，别猜。** 光模块的行为几乎全部写在寄存器里，而 CMIS 的字段含义
高度依赖版本和该模块自己的能力声明——凭印象答「这个位应该是…」十次错好几次。
工具开着的时候有一整套 REST API，直接读回来看。

## 直接问模块

```bash
B=http://127.0.0.1:5000/api

curl -s $B/backends                       # 有哪些适配器可用（含 mock）
curl -s -X POST $B/connect -H 'Content-Type: application/json' \
     -d '{"backend":"ch341","bus":0,"address":80}'
curl -s $B/module/info                    # 厂商 / 型号 / 序列号 / 固件 / 23 项标识
curl -s $B/module/status                  # 模块状态、温度、电压、模块级标志
curl -s $B/module/capabilities            # ★ 这个模块声明了自己能做什么
curl -s $B/module/monitoring              # 逐通道光功率 / 偏置 / DataPath 状态
curl -s $B/module/flags                   # 逐通道标志 + 触发历史
```

其余：`datapath` / `applications` / `thresholds` / `squelch` / `loopback` /
`prbs` / `ber` / `snr` / `counters` / `laser` / `control` / `ext54`。
带 POST 的是控制类（`connect` / `control` / `datapath` / `squelch` /
`loopback` / `prbs` / `laser` / `flags/clear` / `register/write`）。

**`capabilities` 是最该先读的一个。** 下面几节的误读，根子都在
「模块声明了什么」和「界面显示了什么」这两件事上。

裸寄存器（谨慎，见末节）：

```bash
curl -s -X POST $B/register/read -H 'Content-Type: application/json' \
     -d '{"page":17,"address":134,"length":1}'      # page 十进制，17 = 0x11
```

## 界面上最容易误读的六件事

这几条不是工具的怪癖，是 CMIS 本身的设计。看不懂就会得出相反的结论。

### 1　`●!` —— 「现在没事，但它响过」

CMIS 的 Flag 是**闩锁**位：置位后一直保持，**直到有人读了包含它的那个字节**——
而读到它的那一次刷新，也就把它擦掉了。界面每 2 秒刷新一次，所以一次瞬时故障
**只会在某一帧里出现**。

工具替你记着：曾经触发过、现在已恢复的显示
<code>●<sup>!</sup></code>，与「从未触发」的 `●` 区分。
**排查间歇性故障就靠这个**：连上后点一次 **Clear flag history**，让它跑着，
回来看哪些通道带标记。

### 2　`n/a` —— 「这一项根本没人在看」

`01h:157–158` 声明模块实现了哪些 Flag。**没实现的 Flag 读出来是 0，健康通道的
Flag 读出来也是 0**——两者完全一样。所以未实现的一律显示灰色 `n/a`，
不画成绿点。见到 `n/a` 就是：这一项该模块不提供，别当成「正常」。

### 3　`running App N` —— 「你要的和它在跑的不是一回事」

AppSelect 下拉框读的是 **Page 10h 的 Staged Control Set**（你要求的），
模块实际在跑的是 **Page 11h 的 Active Control Set**。平时一致，
**Apply 被拒绝时就会分岔**。不一致的通道下方有红字 `running App N`。

按 CMIS 8.13.3，Apply 分四步：命令受理 → 参数校验 → 执行 → 结果反馈。
**校验不过时模块直接跳过执行步骤**——那条通道继续跑原来的 Application，
数据通道也不会重启。

### 4　置灰的控制 —— 「模块说它没有这个功能」

`01h:155–156`（Table 8-51）逐位声明模块实现了哪些通道控制。未声明的控制
置灰不可勾，悬停写出是哪一位没置位。涉及七个：强制 Tx 静默、禁用自动静默
（收发两侧）、Rx 输出关闭，以及 DataPath 页的 **Tx Enable**、**Tx Pol**、**Rx Pol**。

同理，PRBS 图案下拉框只列 `13h:132–139` 声明的图案，环回只开
`13h:128` 声明的类型。**列表里没有你要的那个图案，是模块没有，不是工具漏了。**

### 5　Tx 偏置的刻度倍数

偏置寄存器数的是 **2 µA 的个数**，但 `01h:160.4-3` 还给它一个 **×1/×2/×4 的倍数**。
16 位 × 2 µA 的上限只有 **131.07 mA**，所以偏置高于这个值的模块（相干模块很常见）
必须声明 ×2 或 ×4。

```bash
curl -s $B/module/capabilities | python -c \
  "import json,sys;print(json.load(sys.stdin)['data']['monitors']['tx_bias_scale'])"
```

读数看着差一倍时，先看这个数。

### 6　标题栏的小图标

不管你停在哪一页，后台每 5 秒轻量轮询一次。发现情况时标题栏出现可点的图标
（点一下跳到 Monitoring）：

| 图标 | 含义 | 出处 |
|---|---|---|
| ⚠ | 当前有监控值超出告警阈值 | Lower 0x09 |
| ↺ | 模块重启过（自上次 Clear 起） | Lower 0x08[0] |
| ↯ N | 有 N 条数据通道弹过（自上次 Clear 起） | 11h:134 |
| ⛔ | **读不到模块了**，屏幕上的一切都不是实时的 | —— |

`⛔` 出现时其余图标会消失：它们描述的是一个现在读不到的模块，
而空白的标题栏会被当成「一切正常」。

## 排查：Apply 点了没生效

按顺序问，别跳步：

1. **提示里说什么** —— 越权的请求在**写入之前**就被拒绝，提示会引用它依据的
   声明寄存器（例如 `channel 9999 is outside the range the module advertises
   for the 100 GHz grid (-40 to 40, 04h:150-153)`）。
2. **Monitoring 页的 Config Status 列** —— `ConfigRejected*` 就是模块拒绝了。
   `ConfigRejectedInvalidAppSel` = 选了它没声明的 AppSelCode；
   `ConfigRejectedLanesInUse` = 有通道不在 DPDeactivated；
   `ConfigRejectedPartialDataPath` = 只给了这个 DataPath 的一部分通道。
   完整代码表见 CMIS 5.4 Table 8-101。
3. **DataPath 页有没有 `running App N`** —— 有就是第 3 条那个分岔。
4. **控制是不是置灰的** —— 见第 4 条。
5. **连点了两次 Apply 吗** —— 上一次还在 `ConfigInProgress` 时到达的新命令，
   按规范被模块**静默丢弃**（不给任何反馈）。等结果出来再点。

## 排查：读数看着不对

1. **先确认这一项模块真的支持** —— `capabilities` 里的 `monitors`
   （温度 / 电压 / Tx 偏置 / 收发光功率 / Aux1-3 / Custom）。不支持的项读回来是 0。
2. **偏置对不上就看刻度倍数** —— 第 5 条。
3. **光功率**是 µW 存储、dBm 显示；0 µW 没有对数，界面按「无光」处理而不是 −∞。
4. **阈值和读数必须同源** —— 两者都在 Page 02h/12h，工具用同一个刻度换算；
   自己拿裸寄存器算的时候容易只换算一边。

## 没有硬件时

内置 7 个 mock，`connect` 时把 `backend` 换成下面任一个即可，不接适配器也能跑通全流程：

| backend | 模拟的模块 |
|---|---|
| `mock_dr8` | 800GBASE-DR8（SMF 500m，EML 1310nm） |
| `mock_sr8` | 800GBASE-SR8（OM4 100m，VCSEL 850nm）—— 故意能力较弱：无逐通道环回、部分 Flag 未实现 |
| `mock_fr4x2` | 2× 400GBASE-FR4（SMF 2km，CWDM4 EML）—— 无强制 Tx 静默、无 Rx 极性翻转 |
| `mock_coherent` | 800GBASE-LR1 相干 lite（DP-16QAM，SMF 10km，802.3dj） |
| `mock_coherent_zr` | 800G 相干可调谐（C 波段 DWDM，ZR 级）—— 偏置 180 mA、声明 ×2 刻度 |
| `mock_1600g_dr8` | 1.6TBASE-DR8（8 × 106.25 GBd PAM4，SMF 500m，802.3dj） |
| `mock_1600g_16lane` | 1.6T 16×100G 主机侧（1.6TAUI-16 C2M，两个 bank） |

后三个是专门用来试边界的：能力较弱的模块、需要刻度倍数的模块、跨 bank 的宽模块。
**主机软件该处理的分支，用这几个 mock 就能全部走到。**

## 裸寄存器读写的两条注意

1. **页切换有时序** —— 写完页选择寄存器要等 **10 ms**（规范的 tBPC）。睡不够会
   读到上一页的内容，表现为间歇性数据错乱。走 API 的 `register/read` 已经处理好了；
   自己拿别的工具直接怼 I2C 时要自己等。
2. **读会改变状态** —— 所有 Flag 都是读后自清。你手动读一遍 `11h:134`，
   界面下一次刷新就看不到那次事件了。排查期间尽量只看界面，别两头同时读。

## 授权与来源

MIT 开源，免费，可商用。
源码与发布：`https://github.com/zhh198903-ctrl/cmis-module-manager`
下载站：`http://106.14.76.130`（右下角对话框可以直接问作者）
