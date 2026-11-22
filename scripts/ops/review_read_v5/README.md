# 审核降读验收工具

`collect_benefit_readonly.py` 在生产服务器本机运行，显式传 `--account 34 --directory <本次私有发布目录>`。只读取固定计量文件和业务表聚合，不调用 Telegram。匿名内存快照不会初始化、续期或改写源文件；缺失字段保留 null。

`validate_benefit.py` 接收符合 `benefit-input.schema.json` 的有界 JSON：

```text
python validate_benefit.py evidence.json
```

没有同账号、同版本、同口径的三组独立完整对照，或者业务完成量、健康时长、到达量、延迟、积压及覆盖证据不完整，结果为 `insufficient_evidence`。它不会把零业务量、缓存命中或预算预留量当成节省。输入最多 2 MiB；只保存有限聚合，不保存群消息、手机号、代理认证或连接字符串。

`diagnostic-codebook.json` 解码固定数字诊断的阶段、模块和函数；行号以对应发布 manifest 的源代码为准。诊断环不会保存异常参数、局部变量或正文，最多保留 23 小时 50 分，周期清理间隔 240 秒。

正式窗口必须从最后一次应用/开关/语义切换后的下一个完整 UTC 小时开始。新计数口径不能和旧运行窗口混用，源码落地、灰度运行及真实收益分别验收。

`followup_six_readonly.py` 的 `reviews` 是全部历史审核行的聚合，不是当前可执行队列。重试时间统一绑定为 UTC 无时区值。最新有效审核的独立聚合尚未在此工具采集，`current_membership_reviews_available=false` 不能解释为当前任务数为零。即使按政策判定 due，仍须应用账号风控、租约、预算和退出等运行门禁，不能称作可执行任务数。保留 `available` 和截断标记；来源失败时不补零，也不把无法证明在线当作实际离线。
