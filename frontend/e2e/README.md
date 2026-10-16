# E2E 全链环境（C-62 入库件）

从零重建并实跑 T58B 全链 E2E（19 条断言）的说明。验证环境：Windows + Git Bash、后端锁定运行时 `.venv`（CC-19，Python 3.11 + FastAPI/SQLAlchemy/Pillow）、E2E venv Python 3.12。

## 1. 前置

- 仓库根已有 `.venv`（后端锁定栈，无需重建；若缺失按仓库根 ENV_REPORT_PYTHON.md + requirements.lock 恢复）。
- Node ≥ 20（vite 8 要求）与 `npm install` 过的 `frontend/`（仓库根有 `package-lock.json`）。

## 2. E2E venv bootstrap（一次性）

```bash
cd frontend/e2e
python -m venv .venv-e2e            # Python ≥3.11 均可（验证时为 3.12）
.venv-e2e/Scripts/python -m pip install -r requirements-e2e.txt
.venv-e2e/Scripts/python -m playwright install chromium   # 浏览器二进制走 Playwright 官方 CDN
```

Linux/macOS 把 `Scripts` 换成 `bin`。`requirements-e2e.txt` 为实测精确 pin：playwright 1.55.0 / greenlet 3.5.6 / Pillow 12.3.0（PIL 用于现场生成上传用 PNG；E2E venv 不需要 requests/httpx，API 调用走 urllib）。

## 3. 运行

```bash
cd frontend/e2e
.venv-e2e/Scripts/python -X utf8 e2e_full_chain.py
echo $?
```

脚本自管临时隔离 DB/storage（`YY_DB_URL`/`YY_STORAGE_ROOT` 指向 tempfile 目录）。默认使用 backend `8101` / frontend `5173`；可用 `YY_E2E_BACKEND_PORT` 和 `YY_E2E_FRONTEND_PORT` 改成一对空闲端口。启动前会检查端口与同一时间运行的 harness 锁；遇到已有服务即 fail closed，不会探测或停止它。启动后要求监听 PID 与本次记录的 backend/frontend `Popen` PID 一致才会发送健康请求。Vite 由 `node .../vite.js` 直接启动，退出只对本次两个 `Popen` 对象执行 terminate/wait，必要时 kill 同一个 PID，不按端口杀进程。

每次运行在 `runs/<run-id>/` 保留 harness stdout/stderr、两个服务各自的 stdout/stderr、`runtime-fingerprint-start.json`、结束时的 `runtime-fingerprint.json`。指纹含 runner/child PID、真实退出码、端口监听 PID、Python/Node 路径及版本、前端源码 hash、E2E harness hash（*.py / README / requirements，不含运行输出或 venv）、dist hash 和 `B-GATE-RELEASE.json` hash；dist 明确标成 Vite dev 未实际提供的产物，缺 Release Gate 则记录 `WAITING_B-GATE-RELEASE`。后端以 `YY_AUTH_STRICT=1` 启动；受保护 urllib API 请求复用 Playwright 登录后的真实 `session` cookie，restore 的旧 cookie 401 探针显式覆盖它，重登录后刷新为新 cookie。默认 `YY_E2E_LOW_MEMORY=1` 限制 Chromium renderer 并关闭 GPU；仅显式设为 `0` 才关闭低内存参数。PowerShell 中用 `$LASTEXITCODE` 查看 Python harness 的真实退出码。

预期输出：19 行 `PASS <断言名>`（boot-backend / seed-invites / boot-frontend / api-seed-invite-admin / api-seed-invite-creator / e2e-must-change-gate / e2e-p2-upload / e2e-asset-approve / api-template-create / api-template-approve / api-seed-blogger-preset / e2e-draft-render / e2e-review-real-image / e2e-review-approve / api-export / api-export-artifacts / api-artifact-download / e2e-p10-audit / e2e-p9-usage），末行 `# summary t58b_e2e: 0 failed []`，退出码 0。任一 FAIL 行会附原因摘要，退出码 1。

最近一次实跑留档：`runs/2026-09-21-t77.log`。

## 4. 已知环境注意（承接 D-T58B-12）

- 默认端口被占用会安全失败；先核实监听 PID 的项目归属，再由对应服务的所有者处理。E2E 不会杀未知监听进程。
- 首次 `playwright install chromium` 需要网络（官方 CDN）；运行本身全程本机回环，脚本内 `safe_url` 强制校验目标为 loopback。
- 改密页为旧密码/新密码/确认三个输入框；P5 草稿/正式提交要求底图有已审批模板与默认署名预设——脚本已通过 API 种子满足。

## Release runtime override

V01/V02 Release 集成运行可设置 `YY_E2E_BACKEND_PYTHON` 指向 Release venv `pyvenv.cfg` 声明的基础 Python，并把 Release 包 `runtime/Lib/site-packages` 设为 `YY_E2E_BACKEND_PYTHONPATH`。该路径只传给 backend child，不污染 E2E Playwright 的依赖导入。Windows uv-created venv 的 `Scripts/python.exe` 是启动器，监听由子 PID 持有；使用其基础解释器可让 D07 仍严格验证 listener PID 与本次 Popen PID 相同。运行指纹记录解释器路径/版本和 backend site-packages 路径；省略时仍默认使用仓库 `.venv`。
