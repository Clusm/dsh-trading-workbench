# SUPERSEDED —— 本包（`c147895/`）已作废，请勿应用

- **作废依据**：`docs/dispatch/reviews/U08/cc9287a/Q_r3.md` §A.2。
- **实测症状**：`05_run_offline_regression_scripts.diff` 在集成树 `b97ae0d` /
  `695023cc` 上 **4 种 git 配置全部 `git apply` 失败**：

  ```
  error: while searching for:
      "scripts/smoke_u02_strategy_position_manager.py",
      # U06a（D 影子研究）：影子最小隔离与旧口径披露。全程离线。
      "scripts/smoke_u06a_shadow_isolation.py",
  )
  EXCLUDED_SMOKE_SCRIPTS = {
  error: patch failed: scripts/run_offline_regression.py:102
  ```

  原因：U14 在 `smoke_u06a` 那一行之后插入了
  `"scripts/smoke_u14_alert_delivery.py",`，本包期望的是紧跟 `)`。

- **后果**：若误用本包，`scripts/run_offline_regression.py` 留在旧态而
  `scripts/smoke_u08_strategy_funnel.py` 已落库 → `_validate_smoke_manifest()`
  直接 `RuntimeError: 未登记冒烟脚本: scripts/smoke_u08_strategy_funnel.py`，
  整个离线回归入口跑不起来。
- **替代**：
  - r2 包 `../ec8fd1c/`（基线 `e50f022`）—— 已在 `695023cc` 上复验 5/5 可应用，
    但**内容已被 r3 取代**（见其 README 顶部标注）。
  - **当前唯一应使用的包：`../0c131f2/`**（基线 = 集成 HEAD `695023cc`）。
