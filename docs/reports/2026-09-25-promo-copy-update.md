# #2 推广素材文案更新

- 时间：2026-09-25 23:47:10 北京时间；目标：`oracle4c24g:/opt/vanguard` 生产数据库。
- 用户确认采用“稳定不降智”，并要求加入“倍率低至 0.09”。将当前 #21 素材复制为 #22，只把 #2 的启用绑定 #17 切换到 #22；#3 的启用绑定 #13 继续使用 #21，推广阶段仍暂停。
- 初版文案（23:56 已替换）：`PP-AI 提供稳定不降智的 AI 模型 API 中转服务，部分模型倍率低至 0.09x，也有使用交流与问题互助群。需要了解接入方式及当前模型、价格，可点击头像查看简介中的官网与交流群，以官网实时说明为准。`
- 文案仍为纯文本，不含群内直接 URL；原账号简介保持官网与自建交流群入口。没有主动触发 Telegram 发送。
- 更新前只读备份：`deploy-artifacts/current-recheck-20260925-2241/creative21-before.json`。事务回执：`promo-copy-update-result.json`。更新后核对：`promo-copy-verified.json` 和 `capacity-after-copy.json`。
- 更新后 #2 已启用绑定 #22，#3 仍绑定 #21。#2 广告额度仍为 30/日，已用 21，素材池数量 1，当前可发目标 0。下一次自然发送前仍须满足群资格、24 小时间隔和广告存活条件；截至核对时没有新版素材的实际 Telegram 发送回执。
- 生产主机访问简介里的官网和 Telegram 交流群均为 HTTP 200。官网首页静态 HTML 未直接展示 `0.09`，实时价格声明来自用户提供的业务信息，本次没有独立核验具体模型的实际计费倍率，也未验证“不降智”的效果指标。
- 初版完整回退（当前需先恢复初版长文案）：将 `deploy-artifacts/current-recheck-20260925-2241/rollback_promo_copy.py` 通过 `ssh -o ProxyCommand=none oracle4c24g 'docker exec -i -e PYTHONPATH=/app vanguard-backend python -'` 执行，事务会在确认 #2 仍绑定新素材且无人新增绑定后恢复 #21 并停用 #22。无需重建镜像。


## 23:56 简化文案

- 用户进一步要求保留 `Pro 低至 0.09x`、`稳定不降智` 和点头像查看简介。仅更新 #22 的正文，不改 #2/#3 绑定或投放额度。
- 当前文案：`PP-AI 中转：Pro 低至 0.09x，稳定不降智。点头像看简介，获取接入方式和交流群入口。`
- 变更时间：2026-09-25 23:56:59 北京时间。更新前：`creative22-before-shortening.json`；事务回执：`shorten-promo-copy-result.json`；更新后读回：`short-copy-verified.json` 和 `short-copy-capacity.json`。
- #2 绑定仍为 #22，#3 仍为 #21；#2 有效广告上限 30/日，更新后已用 21，素材池 1，立即可投群 0；没有手动触发 Telegram 发送，尚无简化版实际发送回执。
- 如需撤销本次简化，执行 `restore_long_promo_copy.py`，它会在确认 #22 正文与绑定没有后续变化后恢复初版长文案。如需进一步回到原 #21，随后再执行 `rollback_promo_copy.py`。

## 9 月 26 日 00:01 末句调整

- 用户将末句简化为“点我头像”。生产 #22 当前完整文案：`PP-AI 中转：Pro 低至 0.09x，稳定不降智。点我头像。`
- 只修改 #22 的 `content`，#2 绑定仍为 #22，#3 仍绑定 #21。更新前备份：`creative22-before-avatar-only.json`；事务回执：`avatar-only-copy-result.json`；更新后读回：`avatar-only-copy-verified.json`。
- 当前一步回退：将 `avatar_only_copy.py` 通过 `ssh -o ProxyCommand=none oracle4c24g 'docker exec -i -e PYTHONPATH=/app vanguard-backend python - restore'` 执行；脚本会先确认文案与绑定未被后续修改。旧的 `restore_long_promo_copy.py` 仅适用于上一版简化文案。
