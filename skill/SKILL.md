---
name: cmis-module-manager
description: 排查和驱动 CMIS Module Manager —— 浏览器里的光模块寄存器工具。用它的 REST API 直接问模块（别凭记忆猜寄存器），看懂界面上几处最容易误读的东西（闩锁标志的 ●! 历史、staged 与 active 配置不一致、置灰的控制、n/a 的标志位、Tx 偏置刻度倍数），判断某次 Apply 为什么没生效、某个读数为什么不对。触发词：CMIS, CMIS Module Manager, 光模块, 模块管理工具, QSFP-DD, OSFP, 寄存器, Page 11h, AppSel, DataPath, ConfigStatus, PRBS, 环回, loopback, 激光器调谐, 闩锁标志, latched flag, CH341, CH347, 光功率, 偏置电流。
---

# CMIS Module Manager

浏览器里的光模块管理工具，对着 OIF CMIS 5.4 写的。跑在
`http://127.0.0.1:5000`，通过 CH341 / CH347 / FTDI 的 USB-I2C 适配器读写模块。
MIT 开源、免费、可商用。

## 装到哪

复制成 `%USERPROFILE%\.claude\skills\cmis-module-manager\SKILL.md`，
新开一个 Claude Code 会话即可生效。装不装都不影响工具本身运行。

**先问模块，别猜。** 光模块的行为几乎全部写在寄存器里，而 CMIS 的字段含义
高度依赖版本和该模块自己的能力声明——凭印象答「这个位应该是…」十次错好几次。
工具开着的时候有一整套 REST API，直接读回来看。

**端口默认 5000，但不一定是 5000**：可以在界面 ⚙ Display 里改，启动时被占用也会换到别的端口。先问清楚：

```bash
curl -s http://127.0.0.1:5000/api/settings/port   # active = 实际端口，conflict = 启动时的冲突
```

5000 上没响应就看控制台打印的地址，或 EXE 旁边的 `cmis_settings.json`。环境变量 `CMIS_PORT` 优先于保存的设置。

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
| ⚠ | 当前有监控值超出告警阈值 | Lower 0x09-0x0B |
| ▲ | Tx 自适应均衡没收敛（自上次 Clear 起） | 11h:0x8A |
| ↺ | 模块重启过（自上次 Clear 起） | Lower 0x08[0] |
| ↯ N | 有 N 条数据通道弹过（自上次 Clear 起） | 11h:134 |
| ⛔ | **读不到模块了**，屏幕上的一切都不是实时的 | —— |

`⛔` 出现时其余图标会消失：它们描述的是一个现在读不到的模块，
而空白的标题栏会被当成「一切正常」。

## 这个 Application 能从哪条通道开始

规范 6.2.3.2.1 是对主机的义务：「**主机必须按模块为该 Application 通告的 Lane Assignment
Options 来分配通道**」。字段是 Application 描述符第 4 个字节 `HostLaneAssignmentOptions`：
位 0-7 对应主机通道 1-8，置 1 表示该 Application 的通道组**可以从这条通道开始**。

DataPath 面板的下拉框每项都写明了，例如 `App 2 — 400GBASE-DR4 4H/4M · begins on 1, 5`。
暂存配置里若有数据通路起始越界，那一行会标 `may not begin here`。

**三条容易搞错的：**

- 规则管的是**起始**。4 通道的 App 从通道 1 起会占用 2-4，主机要把同一个 AppSel 写进
  这四条——后三条是**延续通道**，位图对它们没有规定。拿每条通道去对位图，
  一块合规模块上每四条会误标三条。
- **位图只有 8 位**。超过 8 通道的模块上规范没说这 8 个位怎么延伸，所以检查到通道 8 为止。
- **写入照样放行**，这是有意的：模块会在 `ConfigStatusLane` 里回
  `ConfigRejectedInvalidDataPath`(4h)，看真实模块怎么处理非法分配正是这个工具的用途之一。
  工具只拦模块会**默默吞掉**的写入。

## 排查：Apply 点了没生效

按顺序问，别跳步：

1. **提示里说什么** —— 越权的请求在**写入之前**就被拒绝，提示会引用它依据的
   声明寄存器（例如 `channel 9999 is outside the range the module advertises
   for the 100 GHz grid (-40 to 40, 04h:150-153)`）。
   DataPath 的 Apply 被拒绝（400 / 409）时**一个字节都没写**——
   包括同一请求里的 DPDeinit、Tx 关闭和极性翻转（v2.117.0 之前不是这样：
   拒绝判断在写完之后才做，被拒绝的请求可能已经把数据通路拆掉了）。
2. **Monitoring 页的 Config Status 列** —— `ConfigRejected*` 就是模块拒绝了。
   `ConfigRejectedInvalidAppSel` = 选了它没声明的 AppSelCode；
   `ConfigRejectedLanesInUse` = 有通道不在 DPDeactivated；
   `ConfigRejectedPartialDataPath` = 只给了这个 DataPath 的一部分通道。
   完整代码表见 CMIS 5.4 Table 8-101。
3. **DataPath 页有没有 `running App N`** —— 有就是第 3 条那个分岔。
4. **控制是不是置灰的** —— 见第 4 条。
5. **连点了两次 Apply 吗** —— 上一次还在 `ConfigInProgress` 时到达的新命令，
   按规范（6.2.4.2）被模块**静默丢弃**（不给任何反馈）。工具现在会直接回 **409**，
   点名哪几条通道还在进行中，并且不写触发位。等结果出来再点。
   这条**对所有模块都成立**（不像过渡态那条只针对支持热重配的模块），
   且**按数据通路**判断：另一个端口正在重配不妨碍这个端口；
   上一条被拒绝（`ConfigRejected*`）也不算进行中，可以立即重来。

## 排查：读数看着不对

1. **先确认这一项模块真的支持** —— `capabilities` 里的 `monitors`
   （温度 / 电压 / Tx 偏置 / 收发光功率 / Aux1-3 / Custom）。不支持的项读回来是 0。
2. **偏置对不上就看刻度倍数** —— 第 5 条。
3. **光功率**是 µW 存储、dBm 显示；0 µW 没有对数，界面按「无光」处理而不是 −∞。
4. **阈值和读数必须同源** —— 两者都在 Page 02h/12h，工具用同一个刻度换算；
   自己拿裸寄存器算的时候容易只换算一边。

## 读数可能是 `null`，那不是出错

CMIS 的每一项监控都是可选的，模块在 `01h:159-160` 广告自己有哪些。
**没有的那一项寄存器读出来是 0**，而 0 在这里是很吓人的读数（0 V / 0 mA / 量程底部的 dBm）。

所以这几个字段在模块没有该监控时返回 **`null`**，不是 0：

- `/api/module/status` → `temperature_c`、`voltage_v`，外加 `monitors_present`
- `/api/module/monitoring` → 每条通道的 `tx_power_dbm` / `tx_power_uw` /
  `tx_bias_ma` / `rx_power_dbm` / `rx_power_uw`，外加顶层 `monitors_present`

`monitors_present` 告诉你哪几项存在，用它把「没有这项监控」和「这次读失败」区分开。
拿 `mock_fewmon` 试。

## 激光器调谐也是**按媒体侧通道**索引的

规范 8.15：「Page 12h 的每个 Bank 对应 8 条**媒体侧**通道」。所以 `/api/module/laser` 的 `lanes` 是**媒体侧通道**，不是主机侧。

相干模块只有一路激光器，所以**只有一行**。给不存在的媒体侧通道下调谐会返回 400（依据 `00h:210`），而不是「写成功」。

**超过 8 条通道的模块不受此限制**（8.3.7，同上一节）。

## 这条数据通路走的是哪几条媒介通道

主机选主机侧通道，媒介侧通道是**算出来**的。规范 7.9.1：第一个 Application 实例
（按主机通道编号顺序）用该 Application 通告的、**编号最低且可用**的媒介通道起始的通道组；
第二个用下一个编号最低的可用起始，依此类推。

这个结果不是寄存器（不支持媒介通道交换的模块上），所以 DataPath 面板现在直接算给你看：
每条数据通路写明 `media 1–4`。**这正是 Monitoring 表里衡量它的那几行。**

**为什么要紧**：Monitoring 每一行把「媒介侧的测量值」和「主机侧的状态」放在同一个行号上，
两侧是独立编号的。媒介宽度 = 主机宽度时正好对齐；不相等就错开——
两条 **4H/1M** 的相干数据通路，主机通道 5-8 其实测在**媒介通道 2** 上，
而媒介通道 5 不属于任何一条数据通路。

**三条边界：**

- **只答第一个 bank**。7.9.1：每个 bank 总是 8 条外部媒介通道；8.3.7：超过 8 条通道的模块
  无法无歧义声明自己的媒介通道。跨 bank 怎么延续规范没写，不替它答。
- 媒介宽度超过一个 bank 的数据通路也不给答案。
- 支持媒介通道交换的模块标 **nominal**——实际提交的映射在 `Page 6Dh`。

## Tx 输出禁用和 Tx 静噪也按**媒体通道**算

Table 8-79：`OutputDisableTx`（`10h:130`）、`AutoSquelchDisableTx`（`10h:131`）、`OutputSquelchForceTx`（`10h:132`）的第 i 位是**媒体通道 i**。
`/api/module/datapath` 和 `/api/module/squelch` 的 GET 都附 `media_lanes_present`；POST 时**改动**不存在媒体通道的那一位会 400（原值不变可以照写）。
两行 Rx（`OutputDisableRx`、`AutoSquelchDisableRx`）不在此列。

媒体侧的环回和 PRBS 也一样：`MediaSideOutputLoopbackEnable` / `MediaSideInputLoopbackEnable`（`13h:180/181`，Table 8-131）、媒体侧发生器 / 检测器的使能（`13h:152` / `13h:168`，Table 8-121 / 8-125）的第 i 位是**媒体通道 i**。`/api/module/loopback` 和 `/api/module/prbs` 的 GET 都附 `media_lanes_present`；POST 时改动不存在媒体通道的那一位会 400。PRBS 只判**使能**位；不支持逐通道环回的模块不判（那时任何一位都代表全部通道）。`media_gen_lol_seen` / `media_chk_lol_seen` 对不存在的媒体通道是 `null`，不是「从未失锁」。

## 诊断数据的媒体侧也按**媒体通道**算

`/api/module/snr`、`/api/module/ber`、`/api/module/counters` 的媒体侧字段（`media_snr_db`、`media_ber`、`media_error_count` 等）
对模块**没有**的媒体通道是 `null`，三个接口都附 `media_lanes_present` 列表（依据 `00h:210`）。相干模块上只有第 1 条有值。

计数器的 `*_ber` 为 `null` 表示**一个比特都没计**（没有测量）；`0.0` 表示计了比特、**零误码**。两者别混为一谈。

## Tx/Rx 光功率和偏置是**按媒体侧通道**索引的

Table 8-99 的标题就是「Media Lane-Specific Monitors」。而 Monitoring 的每一行是**主机侧通道** —— 相干模块把 8 条主机侧通道汇进 1 路光载波,所以**只有第 1 条有光功率**,第 2-8 条的寄存器模块根本没有,读出来是 0(= dBm 量程底部,看着像告警)。

`/api/module/monitoring` 每条通道带 `media_lane_present`:为 `false` 时 `tx_power_*` / `rx_power_*` / `tx_bias_ma` 都是 `null`,**不要当成 0 来画**。

依据是 `00h:210`(哪些媒体侧通道不存在)。**超过 8 条通道的模块不用它** —— 规范 8.3.7 说这类模块无法无歧义地声明,所以工具对它们一条都不隐藏。

DataPath State / Config Status 是**主机侧**的,每行照常。

**`Output` 那一列两侧各占一个**:Table 8-95 把 `OutputStatusRx`(`11h:132`)给了
**Rx 输出主机通道**,把 `OutputStatusTx`(`11h:133`)给了 **Tx 输出媒介通道**。
同一格里的两个点说的是模块两侧不同的通道。表头现在标着「Rx host · Tx media」。

`output_valid_tx` 也跟着 `media_lane_present` 走:媒介通道不存在时是 `null`,
**不要当成「输出被静音」**——去查 Tx disable / squelch 会扑空,那条通道根本不存在。
`output_valid_rx` 则每条主机通道都有值。

## 「自适应均衡失败」之前,先看那条通道在不在自适应

规范 Table 6-5 把发送端输入均衡的控制分成互斥的两组,模块按 `AdaptiveInputEqEnableTx` 决定读哪一组:

| 类型 | 控制 | 使能位 |
|---|---|---|
| 自适应 | Freeze(`10h:134`)/ Store(`10h:135-136`)/ Recall(`10h:154-155`) | 1 |
| 非自适应 | HostControlledInputEqTargetTx(`10h:156-159`) | 0 |

**6.2.5.1:模块「忽略与当前设置无关的控制字段值」——两个方向都算。**
Signal Integrity 表按通道把无关的那一格置灰并说明原因,
所以排查 `▲`(Tx 自适应均衡没收敛,`11h:0x8A`)时顺序是:

1. **那条通道的 Tx Adaptive EQ 是不是 On** —— 关着就没有自适应可谈,标志说的是别的事
2. **Tx EQ Adaptation 是 Adapting 还是 Frozen** —— 冻结了就不会再收敛,这是主机自己要的
3. **Tx EQ Recall 调了哪个缓冲** —— 调回来的设置不合当前链路,一样收敛不了

三格都在 Signal Integrity 表上,按通道一行。

**两条别照搬其余列的规矩:**

- `10h:134` 在 Table 8-77 的 Lane-Specific Control 段(129-142,「独立于 Data Path
  状态机**或控制集**」),**写下去即刻生效,不需要 Apply**。这张表其余各列都是暂存的。
- **Recall 不受 ExplicitControl 约束**:6.2.5 明文「在 ExplicitControl 位未置位时同样有效」。
  本工具写入的正是未置位,所以面板上别的暂存值都会被模块自己的值替换,唯独它不会。

Store 是**只写**寄存器,读不回来,表上没有它这一列。

字段能不能用看 `01h:161`:bit 4 是 Freeze,bit 6-5 是可调用的缓冲数(**11b 保留**)。
模块没通告,表上就没有这两列——不是工具漏读。

**`01h:161.6-5` 是个数量,`10h:154-155` 是个编号,两者要比。**
Table 8-54 说 `01b` = 缓冲区数量 1、`10b` = 数量 2;而 Table 8-83 / 8-88 / 8-104
里那两位填的是缓冲区**编号**(`01b` 是 1 号、`10b` 是 2 号)。
所以**只声明一个缓冲区的模块上,「buffer 2」是个不存在的缓冲区**——
Apply 带上它会拿到 `ConfigRejectedInvalidSI`,而这个 recall 又恰恰是
唯一不受 ExplicitControl 约束、每次 Apply 都会真正带上的那个值。
面板会把越界的那一格标红;自己直接读寄存器时记得也比一下。
声明本身是 `11b`(保留)时没有数量可比,那就一个都核对不了。

## 规范第 10 章的等待:等不够时会不会报错

工具里每一处固定等待都对应第 10 章一个参数。**关键差别是等不够的后果**:

| 来源 | 类型 | 等不够 |
|---|---|---|
| Table 10-4 | ACCESS hold-off | **模块拒绝访问**，主机能发现 |
| Table 10-5 | 内容依赖 | **不拒绝**，直接给陈旧数据 |
| Table 10-6 | 条件到标志 | **不拒绝**，标志只是还没立起来 |

Table 10-5 原文：「模块不会阻止访问陈旧数据（即过早的 ACCESS **不会被拒绝**）」。
所以后两类等短了**不会得到错误，而是得到一个数**。

排查「读数莫名其妙」时值得想到这一条：

- **`tDDCS` = 10 ms**（写 `14h:128` 诊断数据选择器之后）。诊断数据区 64 字节的含义
  完全由选择器决定，读早了拿到的是**上一个选择器的字节**，按这一个的格式解出来。
- **`ton_flag` = 200 ms**（条件发生到标志立起来）。读一个还没立起来的标志，
  读到的「没有」和「确实没发生」完全一样。调谐是否被拒绝只有 Page 12h 的标志会说。
- **`tBPC` = 10 ms**（切页/切 Bank），但模块可以在 `01h:169.3-0` 通告更短的值，
  工具按通告值等。这类属于第一种，等不够会被拒绝。

`GET`/`POST /api/module/laser` 的答复里带 `flag_wait_ms`，就是它等了多久才说
「没有通道被拒绝」的依据。

## 通道标志里名字的 Tx / Rx **不等于**哪一侧

`11h:134-153` 二十个通道标志，按名字里的 Tx/Rx 去分侧，**二十个里会错五个**。
Table 8-96 到 8-98 对每行都写明了：

| 字节 | 标志 | 侧 |
|---|---|---|
| 134 | DPStateChanged | host |
| **135** | **FailureFlagTx** | **media**（"affecting media lane"） |
| 136-138 | LOSFlagTx / CDRLOLFlagTx / AdaptiveInputEqFail | host（Tx 的**输入**来自主机） |
| 139-146 | Tx 光功率 / 偏置门限 | media |
| 147-152 | LOSFlagRx / CDRLOLFlagRx / Rx 光功率门限 | media |
| **153** | **OutputStatusChangedFlagRx** | **host** |

**15 个是按媒介通道索引的。** 媒介通道不存在时这 15 个是 `null`，面板显示 `n/a`——
**别当成「已检查、正常」**。尤其 `rx_los` 为 null 不等于「有信号」，是「没有这条光纤」。
5 个主机侧的照常有值。

`/api/module/flags` 每条通道的字典里，**每一个为 true 的键都是一个置位的标志**——
没有别的布尔字段混在里面。要判断媒介通道在不在，用 `/api/module/monitoring` 的
`media_lane_present`。

## 标志亮着但中断没来 = 被屏蔽了

规范一句话定义中断线：**「只要有任何一个标志置位、且它对应的屏蔽位是清零的，Interrupt 就保持有效」**。

`/api/module/status` 的 `interrupt_asserted` 就是这个状态（来自 `Lower 0x03` 位 0，**取值是反的**：规范里叫 InterruptDeasserted）。

有用的是**两者不一致**：**标志置位、`interrupt_asserted` 却是 false，说明那个标志被屏蔽了** —— 模块记下了，但不会去打扰主机。排查「主机为什么没收到告警」时先看这个。

屏蔽位一共**四组**：模块级 `Lower 8-13` → `Lower 31-36`；通道 `11h:134-153` → `10h:213-232`；诊断 `14h:132-139` → `13h:206-213`；**激光器调谐 `12h:231-238` → `12h:239-246`**。

**其中两组默认就是屏蔽的**：激光器调谐（Table 8-109 每个位都写 `Default: 1`）和诊断（Table 8-133 一句话：「本页所有屏蔽位的默认值为 1（屏蔽）」）。另外两组没有规定默认值。所以这两组的标志在没人配置过的模块上**一个都到不了主机**，面板会标 `masked`。

诊断那组还有一处坑：**它只有 8 个字节，不是 18**。两张总览表都写成 18，但明细表说了算——Table 8-138 的标志到 139 为止，Table 8-133 的屏蔽位到 213 为止、214-223 是 Reserved[10]，而 Page 14h 总览自己的下一行也把 140-149 标成 Reserved[10]。那张总览表自相矛盾。

最后一组要单独记两点:**屏蔽位和它管的标志在同一页上**(其余三组都在配对的控制页),而且 Table 8-109 给它每一个位都写着 **`Default: 1`**。所以一块没人配置过的模块,调谐标志一个都到不了主机,面板会在这些标志旁标 `masked`;一条通道六个全屏蔽时,即使这条通道一片安静也会标 `all masked`——那正是没人会想到去查的时候。

`12h:230` 是**调谐标志汇总**,位 <n>-1 置位「当且仅当」该通道在 231-238 里有标志。它是精确定义不是提示,所以模块可能自相矛盾;两个方向是不同的故障,面板会说清是哪一种。

## Aux / Custom 监控的阈值标志：读一次就没了

下部内存 `9-11`（Table 8-9）是**六个模块级监控各自的四个阈值标志**，排法一样（位 3-0 一个监控，位 7-4 下一个）：

| 字节 | 位 3-0 | 位 7-4 |
|---|---|---|
| `Lower 9` | 温度 | 供电电压 |
| `Lower 10` | Aux1 | Aux2 |
| `Lower 11` | Aux3 | 自定义监控 |

**这一整块都是 RO/COR**：读它的那次读就把它清掉了。工具每次轮询一次性读 8-13，所以**你自己再去读一遍只会读到 0**，不是模块正常。要拿这些标志就用 `/api/module/status` 的返回：

- 平铺字段 `aux1_high_alarm` … `custom_low_warn`（六个监控 × 四个级别 = 24 个）
- `aux[]` 里每一项自带 `flags`，跟着它的读数走
- `seen` 记住已经被读走的那些

**模块没声明这项监控时返回 `null`**（同上一节的规矩：不存在的标志读回来也是 0，和“测了，正常”分不开）。

自定义监控（Lower 24-25）的编码规范里写的是“S16 或 U16”，**由厂商自定**，所以工具不给数值，只给它的四个标志。

## Page 14h 是分 Bank 的：选择器也是

规范 8.17：「Page 14h 的每个 Bank 对应 8 条通道」。诊断选择器 `14h:128` 和结果窗口 `14h:192-255` **都在这一页里**，所以**每个 Bank 各有一份**。

自己读诊断数据时：**在哪个 Bank 读，就要先在那个 Bank 里写选择器**。只在 Bank 0 写一次然后遍历读，第 9 条往上的通道拿到的是**那个 Bank 上次留下的窗口**——数值范围看着正常，含义完全不对。

`/api/module/snr`、`/api/module/ber`、`/api/module/counters` 已经替你处理好了；直接用它们就行。

## 写入接口的两条规矩

**所有**写入接口（`control` / `datapath` / `squelch` / `loopback` / `prbs` / `laser` / `media_lane_switching`）都是：

- **没提到的字段保持原值** —— 先读后写，只改你点名的那些。想改一个就只发一个。
- **不认识的字段返回 400**，并列出该接口接受哪些字段。
- **要么全写、要么一个字节都不写** ——所有要写的内容先算好校验完再统一下发。拿到 400 就可以确定模块没被动过，不用回读确认。

**Bank 广播开着的时候**（`Lower 0x1A.7`，Table 8-11，只在 `01h:156.7` 广告时生效），对多 Bank 模块的写入会落到所有 Bank。`datapath` / `squelch` / `media_lane_switching` / `loopback` / `prbs` / `laser` / `acq_counters/reset` 遇到各 Bank 不一致的值都会 400 并指出是哪一项；要么各 Bank 发同一个值，要么先关掉广播。`laser` 只调一条媒体通道也会被拒——那一写会连带改掉其它 Bank 同位置的通道（比如 3 和 11）。

第二条比看起来重要：拼错字段名如果被静默忽略，接口会回「成功」而什么都没改 ——比报错难查得多。PRBS 的嵌套字段（`host_gen` 等四个引擎里面的 `patterns` / `enable_mask` …）同样逐个校验，报错会指明是哪个引擎。

## 读某一页之前，先确认模块有这一页

规范里的坑：**选一个模块没有的页，模块不报错**，而是把 PageSelect 清零、
改成给你 **Page 00h**（上半区是厂商名/料号/序列号的 ASCII）。
所以不看广告位就去读一个可选页，读回来的是文本，解出来的是**看着很像真的**的能力。

`GET /api/module/laser` 因此先看 `01h:155.6`，返回里带 `tunable`：
**`tunable=false` 时不读 Page 04h/12h**，`grids_supported` 为空、
`power_range_dbm` 为 `null`——不是 0，也不是编出来的数。
判断「这块模块能不能调谐」请用 `tunable`，别用 `grids_supported` 是否为空。

## 哪几条通道算同一条数据通道，问服务端

`GET /api/module/datapath` 返回 `datapath_groups`：每组是一条数据通道包含的通道号
（1 起算）。CMIS 要求一条数据通道整体 Apply / Deinit，
所以写 `dp_deinit_mask` 时服务端会把掩码**向上取整到整条数据通道**。

归组规则是**「相邻且 AppSel 相同，最长不超过该应用的主机通道数」**，
不是「按应用宽度整块对齐」——混合配置下两者结果不同。
别自己算，用 `datapath_groups`。

## 换 mock 时标志历史会清零

标志是读一次就清掉的，所以工具自己记一份「出现过什么」（每条通道的 `seen`、
模块级的 `seen`、调谐的 `tuning_flags_seen`，外加 `history_since` 起算时间）。

**这份记录属于当时那块模块**：`connect` 到另一个 `backend` 会清空重新计，
`disconnect` 也会。所以换 mock 对比时不用担心上一块的标志跟过来——
但也意味着<b>换过去之前想留的记录要先读走</b>。

手动清零：`POST /api/module/flags/clear`（只清工具的记录，不写模块）。

## 标志位也分「没有」和「正常」

`GET /api/module/flags` 的 `supported` 现在除了 `01h:157-158` 那六个
（Tx 故障 / 丢失信号 / CDR 失锁 / 自适应均衡失败），
还包含**跟着监控项走的 12 个门限标志**——
`tx_power_*` / `tx_bias_*` / `rx_power_*` 的高低告警与警告，
它们由 `01h:159-160`（监控项广告）决定，模块没有那项监控就没有那些标志。

`GET /api/module/status` 的 `temp_*` / `vcc_*` 八个门限标志同理：
**模块没有对应监控项时返回 `null`，不是 `false`**。`false` 表示「测了，正常」。

判断顺序：先看 `supported[flag]` / 值是不是 `null`，再看真假。

## 裸寄存器读会把锁存标志读没

规范 Table 8-3 里的 **RO/COR**：「一个 RO/COR 字节中的所有位，在该字节被读取之后由模块清零。」
这些是**锁存标志**——模块记录「发生过某件事」的唯一地方，**没有第二份**。

工具里会读它们的面板都会折进标志历史；**裸寄存器面板做不到**（那里只是数字）。
所以读之前会弹确认，写明哪几个字节、记录的是什么；读完转储顶部再说一遍。

**六个块**（后两个工具没有面板，但这个面板能够到）：

| 位置 | 内容 |
|---|---|
| `Lower 8-13` | 模块级标志 |
| `11h:134-153` | 通道标志 |
| `12h:231-238` | 激光器调谐标志 |
| `14h:132-139` | 诊断标志 |
| `17h:128` | Network Path 标志 |
| `2Ch` 整页 | VDM 门限穿越标志 |

**`12h:230` 不在其中**——那个汇总是普通 RO，读它不清任何东西。

`/api/register/read` 的答复里带 `clears_on_read`，按**实际读到的字节**裁剪过。

## 直读直写寄存器要带 Bank

`POST /api/register/read` / `write` 除了 `page` / `address`，还收一个 `bank`（默认 0）。

**按 Bank 分组的页**：`10h–5Fh`、`60h–62h`、`6Dh`、`9Fh`、`A0h–AFh`。
这些页上 **同一个地址在不同 Bank 上是不同通道的寄存器**，
Bank b = 第 `8b+1` 到 `8b+8` 条通道。通道数超过 8 的模块，不带 `bank` 就只能读到前 8 条。

```bash
curl -s -X POST $B/api/register/read -H 'Content-Type: application/json' \
     -d '{"page":18,"address":136,"length":2,"bank":1}'    # 12h:136, 第 9-10 条通道
```

返回里带 `bank`、`banked`（这一页分不分 Bank）、`banks`（本模块有几个 Bank）。

## 媒体通道切换:禁用时的 Commit 会被静默吞掉

Table 8-196:`EnableMediaLaneRedirection`(`6Dh:152` bit 0)为 0 时,
「commit 命令**没有效果**」——不是拒绝,模块**不写任何结果码**。
所以 `POST /api/module/media_lane_switching` 带 `commit:true` 时,
若按本次请求的 `enable` 算下来仍有任何一组是禁用的,会**直接 400**,且不写任何东西。
要提交就在同一请求里带 `enable:true`,或先单独启用。

`ext54` 里每条通道的 `commit_result_kind` 是 `none` / `success` / `in_progress` /
`rejected`(码 3–6)/ `reserved`(> 6),按类判断,别自己记码值。

## 通道极性:同样八个位,在宽模块上是三个意思

`01h:171-172`（Table 8-57）只有八个位。规范 **8.4.13** 说，模块**多于八条通道**时
这八个位「适用于**每一组八条通道**」，**除非**它声明支持在 Page 60h 上逐通道说明。
所以要先看通道数和 `01h:174.7`，再决定这一行在说谁：

| 情况 | 这八个位是什么 |
|---|---|
| ≤ 8 条通道 | 就是这个模块的通道 |
| 更宽 + 有 Page 60h | 只是**第一组八条**；逐通道实际状态在 `60h:128-129`（8.30.1） |
| 更宽 + 没有 Page 60h | 第 1 条那一位**同时也是**第 9、第 17 条的 |

**第三种最容易出事**：一个 16 通道、没有 Page 60h 的模块说「第 1 条反接」，
意思是第 9 条也反接。只读八条就等于**对一半通道一无所知**。
`/api/module/capabilities` 现在直接给 `default_polarity`（已按适用通道铺开）和
`default_polarity_scope`（`module` / `first_lane_group` / `every_lane_group`），别自己推。

**Page 60h 的极性是分 Bank 的**，每个 Bank 八条通道，自己拼接时记得**跨 Bank 重新编号**
——各 Bank 的原始字节都是从 1 号通道数起的。`/api/module/ext54` 的 `polarity_status`
已经编好 1…N。

> `60h:128-129` 是 **RO**（Table 8-188），不是标志、不锁存、读了也不清除。

## 规范自己的矛盾:极性寄存器的地址

8.4.13 和 8.30.1 的正文都把这两个字节写成 **`01h:172-173`**，
但同一节里的 **Table 8-57 定义的是 171 和 172**。**以表为准**（171-172）。
按正文写会整体偏一个字节，读到 `DefaultOutputPolarityRx` 和它后面那个保留字节。

**这三种会直接报 400，不会默默给 Bank 0**：bank 超出模块的 Bank 数；
在不分 Bank 的页上给了非 0 的 bank；在下半区（`0x00–0x7F`）给了 bank。

## 接真适配器

先 `curl -s $B/backends` 看 `available`，它会把不可用的原因一并说清楚。

| 适配器 | backend | 开箱即用？ | 说明 |
|---|---|---|---|
| **CH341** | `ch341` | 是 | 装 WCH 驱动即可，用驱动自带的 DLL |
| **CP2112** | `cp2112` | 是，且**连驱动都不用装** | HID 设备，Windows 用自带的 hidclass 驱动 |
| **MCP2221 / MCP2221A** | `mcp2221` | 是，且**连驱动都不用装** | 同上 |
| **CH347** | `ch347` | 装了驱动就行 | 若显示不可用，多半是驱动包没带 `CH347DLL.dll`，从 WCH 的 CH347EVT 包里取一个放到 EXE 同目录 |
| **FTDI** | `ftd2xx` | 装 FTDI 官方驱动即可 | 走 D2XX，用 FTDI 自己的驱动，**不需要 Zadig** |
| **FTDI** | `ftdi` | 否 | 走 pyftdi + libusb，Windows 上需要用 Zadig 把 FTDI 驱动换成 WinUSB，会影响机器上其它 FTDI 软件 |

**能报「没模块应答」的是哪几个**：CP2112 / MCP2221 / FTDI 直接拿得到 ACK 位，
读不到模块会直接报错。**只有 CH341 拿不到**（它的接口看不到缺失的 ACK），
所以 `connect` 才要额外探一次总线，见下。

**客户版 EXE 是 32 位的，这是有意的**：WCH 驱动装进系统的 `CH341DLL.dll` 是
32 位的，而 64 位进程加载不了 32 位 DLL（Windows 的限制）。32 位 EXE 才能直接
用驱动自带的那个 DLL，用户不用额外下载或放置任何文件。

### connect 返回 502 = 总线上没有模块应答

空的 I2C 总线被上拉电阻拉高，**每个字节都读回 `0xFF`**，而 CH341 的接口看不到
缺失的 ACK、会把这次读当成成功。所以 `connect` 会先探低位内存前 3 字节，
**全 `FF` 或全 `00` 就拒绝连接**（HTTP 502），并在 message 里写出读到了什么。

碰到 502 别去查工具，按这个顺序查：模块有没有插到位 → 适配器的 SDA/SCL 有没有
接对 → 地址是不是 `0x50`。

> 在此之前工具会把那串 `FF` 当数据解析，界面上会出现一只**根本不存在的模块**：
> `CMIS 15.15`、256 通道、模块状态 `Reserved`、厂商名乱码、温度 `-0.0039 °C`。
> 如果你看到这组数字，说明用的是旧版本，升级即可。

## 没有硬件时

内置 10 个 mock，`connect` 时把 `backend` 换成下面任一个即可，不接适配器也能跑通全流程：

| backend | 模拟的模块 |
|---|---|
| `mock_dr8` | 800GBASE-DR8（SMF 500m，EML 1310nm） |
| `mock_sr8` | 800GBASE-SR8（OM4 100m，VCSEL 850nm）—— 故意能力较弱：无逐通道环回、部分 Flag 未实现 |
| `mock_fr4x2` | 2× 400GBASE-FR4（SMF 2km，CWDM4 EML）—— 无强制 Tx 静默、无 Rx 极性翻转；**两个端口可同时重配**（6.2.4.2 的「配置进行中忽略新触发」按数据通路算，不是整个模块）|
| `mock_coherent` | 800GBASE-LR1 相干 lite（DP-16QAM，SMF 10km，802.3dj） |
| `mock_coherent_zr` | 800G 相干可调谐（C 波段 DWDM，ZR 级）—— 偏置 180 mA、声明 ×2 刻度 |
| `mock_1600g_dr8` | 1.6TBASE-DR8（8 × 106.25 GBd PAM4，SMF 500m，802.3dj） |
| `mock_1600g_16lane` | 1.6T 16×100G 主机侧（1.6TAUI-16 C2M，两个 bank） |
| `mock_24lane` | 24 通道 / 三个 bank —— 走 CMIS 5.4 的通道数逃逸路径 |
| `mock_zr16` | 16 通道可调谐 —— Page 12h 有第二个 bank（调谐页按介质通道分 bank，每 bank 8 条）|
| `mock_fewmon` | 只实现部分监控项（`01h:159-160`）—— 没有电压/发送光功率/偏置电流 |

后六个是专门用来试边界的：能力较弱的模块、需要刻度倍数的模块、跨 bank 的宽模块。
**主机软件该处理的分支，用这几个 mock 就能全部走到。**

## 裸寄存器读写的三条注意

1. **页切换有时序** —— 写完页选择寄存器要等 **10 ms**（规范的 tBPC）。睡不够会
   读到上一页的内容，表现为间歇性数据错乱。走 API 的 `register/read` 已经处理好了；
   自己拿别的工具直接怼 I2C 时要自己等。
2. **读会改变状态** —— 所有 Flag 都是读后自清。你手动读一遍 `11h:134`，
   界面下一次刷新就看不到那次事件了。排查期间尽量只看界面，别两头同时读。
3. **别把读回来的字节整个写回去** —— `Lower 26.3` 的 SoftwareReset 是 **WO/SC**
   （Table 8-11）。Table 8-3 说它读出来是 0，**除非读得太早**：模块还没评估并清掉
   刚写进去的非零位。字节 `0x1A` 把几个不相干的控制打包在一起，所以改一个位通常要
   先读再合并——合并时**必须先把第 3 位剥掉**，否则一次赶上瞬态的读会把它写回去，
   在你改低功耗的中途又复位一次模块。规范没给这个窗口任何时间，等不过去。

## 授权与来源

MIT 开源，免费，可商用。
源码与发布：`https://github.com/zhh198903-ctrl/cmis-module-manager`
下载站：`http://106.14.76.130`（右下角对话框可以直接问作者）
