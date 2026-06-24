# 告警 SSOT — 已并入 ops.observability

> **MOVED**: 告警(规则、分级、飞书路由、watchdog、in-band 探针、route canary、所有 SOP)已收敛到可观测性单一 owner:
> **[docs/ssot/ops.observability.md](./ops.observability.md)**。
>
> 原因:CI/CD-环境那次收敛同一道理——「可观测性」是一个概念,被采集/告警/报告分着拥有就会互相漂移
> (例如投递 canary 这种"报告"被塞进"告警"路径)。归一到一个 owner,以时间尺度分层(分钟/小时/天/月 × alert/report)
> 为统一框架,见 ops.observability §2、§5、§7,以及 issue #425。
>
> 此文件仅作重定向保留,避免历史链接 404。
