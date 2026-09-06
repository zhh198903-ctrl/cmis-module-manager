# 安装这个 skill

把 `cmis-module-manager` 这个文件夹整个放进 Claude Code 的技能目录：

```
%USERPROFILE%\.claude\skills\cmis-module-manager\SKILL.md
```

也就是说复制完长这样：

```
C:\Users\<你>\.claude\skills\cmis-module-manager\
    SKILL.md
    INSTALL.md
```

新开一个 Claude Code 会话即可生效。装好之后可以直接问它：

- 「我点了 Apply 但配置没生效，帮我看看为什么」
- 「这个通道的标志位显示 n/a 是什么意思」
- 「为什么 Tx Pol 这个框是灰的」
- 「帮我读一下现在连着的模块什么状态」
- 「偏置电流读数看着差一倍，对不对」
- 「我要跑 PRBS，图案列表里为什么没有 PRBS23」

工具开着的时候，它会通过本机的 REST API 去读**真实模块**再回答，
而不是凭印象讲 CMIS 应该怎样。

装不装都不影响 CMIS Module Manager 本身运行——这只是让 Claude Code 懂得
怎么驱动它、怎么解释它显示的东西。

---

# Installing this skill

Copy the `cmis-module-manager` folder to
`%USERPROFILE%\.claude\skills\cmis-module-manager\`, then start a new Claude
Code session. It teaches Claude Code to drive CMIS Module Manager through its
local REST API and to explain the parts of the interface that are easy to
misread — latched flags, staged versus active configuration, controls the
module does not advertise, and the Tx bias scaling factor.

CMIS Module Manager runs fine without it.

MIT licensed, free, commercial use permitted.
