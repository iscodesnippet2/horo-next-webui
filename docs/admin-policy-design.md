# Admin policy：設計草案

狀態：已實作於 `horo_overlay.py` 第 8 節（預定 0.52.544.5）。第 4～6 節的 anchor 已對 release tag `v0.52.544` 與上游 `master`（`382da96`）套用查證；實作與草案的差異見第 11 節。

## 1. 要解決的問題

部署方式是**每位使用者用自己的 OS 帳號跑一個 WebUI**，設定檔在
`~/.hermes/webui/settings.json`，歸使用者所有。

上游 Settings 已經能隱藏這兩類東西：

- **側欄分頁**（`hidden_tabs`）：`mindmap` `skills` `memory` `tasks` `kanban` `workspaces` `profiles` `insights` `logs` `todos`。上游寫死 `chat`、`settings` 不能隱藏。
- **輸入框控制項**（`hide_composer_*`，共 15 個）：`attach` `mic` `voice_mode` `saved_prompts` `model` `reasoning` `toolsets` `yolo` `profile` `workspace` `status` `context` `quota_chip` `bg_badge` `mobile_config`。

但這些存在使用者自己的檔案裡，使用者在 Settings 點一下就恢復，admin 無法統一控制。

目標是讓 admin 透過一個**使用者無法寫入**的檔案：

1. 強制某些設定值（例如隱藏 YOLO、toolsets 開關），使用者在介面上改不動。鍵名沿用上游的 `hidden_tabs`、`hide_composer_*`，不另外發明。
2. 強制隱藏某些側欄分頁（例如 kanban、logs），包括 `settings`。
3. 選擇性封鎖某些功能的 API，讓「隱藏」不只是畫面上看不到。

使用者可以自行調整哪些設定（例如主題、字體），v1 先不處理：admin 可以整個隱藏 settings 分頁，之後再細分。

## 2. 非目標與信任邊界

- WebUI 用使用者自己的帳號執行，使用者掌控 process、環境變數與安裝的套件。他可以另外裝一份沒有這個 overlay 的 WebUI 來繞過。**這是介面治理，不是安全邊界。**
- 真正需要強制的能力（危險工具、YOLO 自動核准、對外連線）要在 WebUI 之外處理：OS 權限、sandbox、網路規則、agent 端由 admin 管理的設定。只藏 WebUI 的按鈕，`horo` / `hermes` CLI 照樣能做同樣的事。
- v1 不做 per-user 或 per-group 的差異。一份 policy 套用到讀它的所有 WebUI。

## 3. Policy 檔

### 位置

1. 有設環境變數 `HORO_WEBUI_POLICY`：讀它指定的路徑，例如共享資料夾 `/mnt/share/horo/webui-policy.json`。
2. 沒設：讀預設路徑 `/etc/horo/webui-policy.json`。

**policy 是必要的。** 每個 WebUI 都要找得到一份合法的 policy，否則一律擋下（見下表）。不需要每台機器各放一份：

| 部署方式 | 每台機器要做的事 | policy 檔 |
|---|---|---|
| 共享資料夾 | 設好 `HORO_WEBUI_POLICY`，且連得到共享資料夾 | 全部機器共用一份 |
| 本機 | 在 `/etc/horo/webui-policy.json` 放一份 | 每台各一份 |

admin 不想限制任何東西時，也要放一份只有 `{"version": 1}` 的檔案。

使用者能改自己的環境變數，但各種改法都會被擋下：

| 使用者的做法 | 結果 |
|---|---|
| 拿掉 `HORO_WEBUI_POLICY` | 改讀預設路徑；沒有檔案就擋下 |
| 指到不存在的檔案 | 擋下 |
| 指到自己寫的檔案 | 檔案歸自己或自己能寫，權限檢查不過，擋下 |

剩下的繞法是另外裝一份原版 WebUI，見第 2 節。

### 載入規則

| 狀況 | 行為 |
|---|---|
| 找得到、讀得到、合法、權限正確 | 套用 |
| 找不到檔案（不論是環境變數指定的路徑還是預設路徑） | **fail closed** |
| 讀不到（含共享資料夾斷線）、JSON 壞掉、出現未知的鍵／功能名／分頁名、型別錯誤 | **fail closed** |
| policy 檔歸執行 WebUI 的使用者所有，或該使用者能寫入 policy 檔或它所在的資料夾 | **fail closed**：否則使用者能改掉或換掉 policy |

**fail closed** 是指：除 `/health`、`/login`、`/api/auth/*` 外，所有 `/api/*` 回 503，訊息寫明 policy 路徑與錯誤原因，並記到 log。WebUI 仍會啟動，修好 policy 後不必重啟就能恢復。

權限檢查不要求擁有者是 root：網路磁碟上的檔案常常不是 root 擁有，例如 NFS 的 root squash 會把 root 對應成 nobody。改成檢查「不歸使用者本人，且使用者對檔案與所在資料夾都沒有寫入權限」（`os.access(..., os.W_OK)` 為假）。資料夾也要檢查，否則使用者能刪掉檔案換成自己的。

共享資料夾連不上時不沿用最後一次讀到的 policy：沿用等於斷線時 admin 失去控制。

每個請求檢查檔案的 `mtime`，有變動才重讀，路徑每次都重新解析。admin 改完 policy 不必重啟 WebUI。

### 格式

```json
{
  "version": 1,
  "settings": {
    "hide_composer_yolo": true,
    "hide_composer_toolsets": true,
    "check_for_updates": false
  },
  "hide_tabs": ["kanban", "logs", "insights", "settings"],
  "block_features": ["kanban", "terminal"],
  "allow_commands": ["help", "new", "model", "skills"]
}
```

| 鍵 | 意義 |
|---|---|
| `version` | 必填，目前只接受 `1` |
| `settings` | 強制值。鍵必須在上游 `_SETTINGS_ALLOWED_KEYS` 內，且不能是 `hidden_tabs`、`tab_order`、`password_hash`、`_set_password` 等。強制值會影響所有讀 `load_settings()` 的地方，包括伺服器端的行為（例如 `check_for_updates`、`api_redact_enabled`） |
| `hide_tabs` | 強制隱藏的分頁，與使用者自己隱藏的取聯集。**`chat`、`skills` 不可列入**；`settings` 可以。可用名稱：`mindmap` `memory` `tasks` `kanban` `workspaces` `profiles` `insights` `logs` `todos` `settings` |
| `block_features` | 封鎖該功能的 API（403），並自動加入 `hide_tabs`。只接受第 5 節表格裡的名稱 |
| `allow_commands` | slash 指令白名單（第 12 節）。**沒有這個鍵**：所有指令照上游行為；**有這個鍵**：只有列出的指令（和永遠可用的 `/help`）能用 |

使用者自己能隱藏哪些分頁維持上游行為（不能藏 `chat`、`settings`）：只有 admin 能藏 `settings`，使用者不會把自己鎖在設定頁外。

## 4. 設定的強制方式（後端）

### 做法

包住上游的 `load_settings` 與 `save_settings`，**讓所有讀設定的地方都看到 policy 值**，但 policy 值**永遠不寫進使用者的 `settings.json`**。

| 情境 | 行為 |
|---|---|
| 讀取 | 上游讀到的值，用 `settings` 的強制值覆蓋，`hidden_tabs` 與 `hide_tabs` 取聯集 |
| 寫入 | 丟掉 body 中被鎖的鍵；從 `hidden_tabs` 移除 policy 加上的分頁，只保留使用者自己的選擇。上游寫完之後，回傳值再套一次 policy |
| 撤掉 policy | 由於 policy 是必要的，撤掉會被擋下；換一份 policy 時，使用者的檔案裡仍是他原本的選擇 |

### Anchor：`api/config.py`

| # | 原文 | 改成 |
|---|---|---|
| C1 | `def load_settings() -> dict:` | `def _horo_upstream_load_settings() -> dict:` |
| C2 | `def save_settings(settings: dict) -> dict:` | `def _horo_upstream_save_settings(settings: dict) -> dict:` |
| C3 | 位於 `save_settings` 內：`persisted_speech_keys = _extract_persisted_speech_keys(raw_settings)` 下一行的 `current = load_settings()` | `current = _horo_upstream_load_settings()`。不改的話，C1 包過的值會被寫進檔案 |
| C4 | 檔尾附加 | `load_settings = horo_policy.wrap_load(_horo_upstream_load_settings)`、`save_settings = horo_policy.wrap_save(_horo_upstream_save_settings)` |

`config.py` 內部以及 `routes.py` 在 import 時抓到的 `load_settings` 名稱，都會指向包過的版本。`routes.py` 是在 `config.py` 執行完之後才 import，所以拿到的是包過的版本。

上游清理設定時會從 `hidden_tabs` 丟掉 `chat`、`settings`（`if k in {"hidden_tabs", "tab_order"} and s in {"chat", "settings"}:`）。policy 的 `settings` 分頁是在 C4 包過的讀取時才加入，不經過這段清理，所以這裡不必改；實作時要確認清理只發生在讀檔與存檔，不在 C4 之後。

### Anchor：`api/routes.py`

| # | 原文 | 改成 |
|---|---|---|
| R1 | GET `/api/settings` 內的 `settings["persisted_speech_keys"] = persisted_speech_settings_keys()` | 下面加一行 `settings["horo_policy"] = horo_policy.client_view()`，列出被鎖的鍵與分頁，給前端顯示鎖頭用 |

`horo_policy` **不能**由 `load_settings` 回傳，否則會被 `save_settings` 寫進檔案（上游會把 `current` 的所有鍵寫回）。

## 5. API 封鎖（後端）

### 做法

在 `server.py` 認證通過之後、分派到 `handle_*` 之前加一道檢查。這樣 GET、POST、PUT、PATCH、DELETE 都會經過，只需要兩個 anchor。

| # | 原文 | 改成 |
|---|---|---|
| S1 | `do_GET` 中 `if not check_auth_or_close(self, parsed): return` 的下一行 `result = handle_get(self, parsed)` | 先檢查 `horo_policy.refusal(parsed.path)`，被封鎖就回 403 |
| S2 | `_handle_write` 中 `if not _is_csp_report_post and not check_auth_or_close(self, parsed): return` 的下一行 `result = route_func(self, parsed)` | 同上，另外先呼叫 `arm_connection_close_if_body_pending`，避免沒讀完的 body 汙染 keep-alive 連線（mindmap 的 `X-Horo-Profile` 也是這樣處理） |

policy 找不到或不合法時的 503 也走這兩個 anchor。

### 功能與 API 路徑對照（overlay 維護）

v1 只開放「聊天主流程用不到」的功能。每一項都要在實作時用 browser smoke 驗證聊天、session、workspace 仍正常。

| 功能名 | 封鎖的路徑前綴 | 對應分頁 | 待驗證 |
|---|---|---|---|
| `kanban` | `/api/kanban` | kanban | |
| `tasks` | `/api/crons` | tasks | |
| `memory` | `/api/memory` | memory | |
| `insights` | `/api/insights` | insights | |
| `logs` | `/api/logs` | logs | |
| `terminal` | `/api/terminal` | （無） | |
| `mcp` | `/api/mcp` | （無） | |
| `extensions` | `/api/extensions`、`/api/plugins`、`/api/dashboard` | （無） | |

**v1 刻意不開放**：

- `skills`：分頁永遠顯示，API 不能封鎖，否則分頁會壞掉。
- `workspaces`（`/api/workspaces`、`/api/file`、`/api/git`、`/api/upload`）、`profiles`（`/api/profile*`）、`/api/session*`、`/api/chat*`、`/api/settings`：聊天、mindmap 和 WebUI 自身都依賴這些路徑，只能用 `hide_tabs` 隱藏介面。

前綴比對要用 `path == prefix` 或 `path.startswith(prefix + "/")`。例如 `/api/memory` 不能誤擋 `/api/memoryX`。

上游新增功能時，對照表不會自動更新，新功能預設不受控。這是逐項加入的白名單，不打算做「全部預設封鎖」，以免擋到聊天。

## 6. 前端

### 隱藏

後端讀設定時已經回傳聯集後的 `hidden_tabs` 與強制後的 `hide_composer_*`，上游既有的套用流程大致不必修改。boot 時先套用 localStorage 的快取，拿到 `/api/settings` 後再校正，這和上游行為一致。

### 讓 admin 能隱藏 settings

上游在三處寫死 `chat`、`settings` 永遠顯示，`settings` 被 policy 隱藏時都要放行（使用者自己的 `hidden_tabs` 仍不能藏它）：

| # | 檔案與原文 | 改成 |
|---|---|---|
| T1 | `static/panels.js`：`const _ALWAYS_VISIBLE_TABS = new Set(['chat','settings']);`（出現 1 次，另有 4 處引用） | 引用處改成「在這個集合裡，且不是被 policy 隱藏的分頁」 |
| T2 | `static/index.html`：flash-prevention inline script 的 `var fixed=new Set(['chat','settings']);` | 第一次畫面時 policy 還沒載入：localStorage 快取裡由 policy 隱藏的 `settings` 也要能套用，拿到 `/api/settings` 後再校正 |
| T3 | `api/config.py` 的 `hidden_tabs` 清理 | 見第 4 節，不經過，原則上不改 |

另外要確認 settings 分頁被藏起來後，沒有其他入口能開啟它（鍵盤快捷鍵、URL、其他按鈕呼叫 `switchPanel('settings')`）。有的話一併擋住，否則只是藏了 rail 上的按鈕。

### 鎖住開關

| # | 檔案與原文 | 改成 |
|---|---|---|
| P1 | `static/panels.js`：`function _toggleTabVisibilityChip(panel){` | 開頭：分頁被 policy 鎖住就直接 return |
| P2 | `static/panels.js`：`function _toggleComposerControlChip(key){` | 開頭：鍵被鎖住就直接 return |
| P3 | `static/panels.js`：`_renderTabVisibilityChips` 裡的 `chip.onclick=function(){` 區塊，anchor 要包含 `      _toggleTabVisibilityChip(panel);` 這一行（`_tabVisibilityDragSuppressUntil)return;` 單獨出現兩次，不能單獨當 anchor） | 前面加上：被鎖住時加 `chip-locked` class、`aria-disabled="true"`、title「由管理員設定」 |
| P4 | `static/panels.js`：`chip.onclick=function(){if(Date.now()<_composerControlDragSuppressUntil)return;_toggleComposerControlChip(def.key);};`（**兩處**，`count=2`） | 同 P3 |
| P5 | `static/index.html`：在 `panels.js` 的 `<script>` 之前插入 `horo_policy.js` | 提供 `window._horoPolicyLocked(kind, name)`，資料來自 `/api/settings` 的 `horo_policy` |

`chip-locked` 的樣式放進新檔 `static/horo_policy.css`，跟 mindmap 一樣用 `<link>` 掛上。

v1 的限制：`settings` 裡不是 chip 的鍵（例如 `check_for_updates` 的 checkbox）由伺服器強制，但介面上不顯示鎖頭。使用者勾了之後，存檔回傳的是強制值，畫面會跳回去。要顯示鎖頭，得逐一對應「設定鍵 → 表單元素 id」，留到 v2。

### Policy 失效時的畫面

503 時前端要顯示明確的訊息（policy 路徑與原因，或請聯絡管理員），而不是上游一般的錯誤 toast。

## 7. 檔案與測試

### 新增檔案

- `overlay/policy/horo_policy.py`：複製到 `api/horo_policy.py`。負責路徑解析（`HORO_WEBUI_POLICY` → 預設路徑）、載入與驗證、權限檢查、mtime 快取、`wrap_load`、`wrap_save`、`client_view`、`refusal`
- `overlay/policy/horo_policy.js`、`overlay/policy/horo_policy.css`
- `overlay/tests/test_policy.py`

`horo_overlay.py` 新增第 8 節「Admin policy」，寫法比照第 7 節 mindmap。全部是 exact anchor，任何一處對不上就 build 失敗。

### 測試

測試透過 `HORO_WEBUI_POLICY` 指向暫存檔，不需要 root，也不需要改模組常數。暫存檔要放在測試使用者無法寫入的位置，或在測試中模擬權限檢查的結果，否則權限檢查會擋下。實作時決定採哪一種。

1. **單元測試（`test_policy.py`）**：
   - 環境變數指定的檔案、預設檔：存在且合法、不存在、讀不到、JSON 壞掉、未知鍵、未知功能名、`hide_tabs` 含 `chat` 或 `skills`
   - 沒設環境變數且沒有預設檔：擋下
   - 歸使用者所有、使用者可寫入檔案、使用者可寫入資料夾：擋下
   - `hidden_tabs` 取聯集、存檔時去掉 policy 分頁、policy 可以藏 `settings` 但使用者自己不行
   - 被鎖的鍵寫不進去，且 policy 值不會出現在 `settings.json`
   - 前綴比對的邊界情況
2. **`verify_wheel.py`**：從安裝好的 wheel 啟動 server，然後確認：
   - 沒有 policy 時 `/api/*` 回 503，`/health` 正常
   - `/api/settings` 回傳強制值與 `horo_policy`
   - POST 改被鎖的鍵無效
   - 被封鎖的路徑回 403，`/api/session*`、`/api/chat*` 不受影響
   - policy 壞掉時回 503，修好後不重啟即恢復
3. **`browser_smoke.py`**：被鎖的 chip 點了不會變；policy 隱藏的 `settings` 分頁打不開；聊天流程正常。
4. 既有的 `verify_wheel.py`、`browser_smoke.py` 檢查都要先放一份 policy 才能跑，因為沒有 policy 就會被擋下。

## 8. 其他要改的地方

- `README.md` 的 Principles 寫的是「Packaging and launch, plus one feature」，要改成兩個功能，並加一節說明 admin policy、它是必要的，以及它的信任邊界（第 2 節）。
- `pypi/README.md` 要寫清楚：**安裝後必須放一份 policy，WebUI 才能使用**，並附最小範例 `{"version": 1}` 與 `HORO_WEBUI_POLICY` 的用法。這改變了 horo-next-webui 裝完即可用的預設行為。
- `docs/security-review.md` 補一段：policy 是介面治理，不是安全邊界。

## 9. 已決定

1. policy 是必要的：找不到、讀不到、不合法、權限不對，一律 503，不沿用舊的 policy。
2. 路徑由 `HORO_WEBUI_POLICY` 指定，可放共享資料夾；沒設就讀 `/etc/horo/webui-policy.json`。
3. `chat`、`skills` 永遠顯示；admin 可以隱藏 `settings`。`skills` 不列入可封鎖的功能。
4. 鍵名沿用上游的 `hidden_tabs`、`hide_composer_*`。
5. 使用者可以自行調整哪些設定，v1 先不處理。

## 10. 待決事項

1. `block_features` 第一批要開放哪些功能。
2. 是否需要 v2 的 per-group policy（例如依 OS group 套用不同的 policy）。
3. agent 端（horo-next）是否要有對應機制，例如由 admin 統一管理的 toolsets 白名單。這不屬於 WebUI 的範圍，但真正的限制要靠它。
4. 測試時如何滿足權限檢查（第 7 節）。

## 11. 實作紀錄

與草案的差異與補充，以程式為準：

- **C5（新增）**：`config.py` 模組層級的 `_startup_settings = load_settings()` 在 C1 改名後會找不到名字，而且它會把結果寫回 `settings.json`。改成讀 `_horo_upstream_load_settings()`，policy 值不會在啟動時被寫入。overlay 的 postcondition 另外要求 `config.py` 內不能再有直接呼叫 `load_settings()` / `save_settings()` 的地方（註解除外），上游新增呼叫時 build 會失敗，逼人決定它在 policy 的哪一側。
- **C4**：附加在檔尾，並呼叫 `horo_policy.configure(...)` 傳入上游的設定 schema（允許的鍵、預設值、bool／enum／int／float 範圍）。`settings` 的強制值依這份 schema 檢查型別；schema 沒設定時視為 policy 不合法（fail closed）。
- **wrap_save**：只丟掉「policy 隱藏、且使用者自己原本沒隱藏」的分頁。使用者自己也隱藏的分頁仍保留在他的 `hidden_tabs`，policy 撤掉後維持他的選擇。
- **policy 無法使用時**：`load_settings` 照常回傳使用者的值（`/login` 要讀密碼 hash），`save_settings` 除了 `_set_password` / `_clear_password`（policy 不能鎖的鍵，登入時升級舊 hash 會用到）以外一律丟 `PolicyUnavailable`。
- **503 的範圍**：草案寫「所有 `/api/*`」。實作改成白名單：只放行頁面外殼與靜態檔（`/`、`/index.html`、`/sessions`、`/session/*`、`/static/*`、manifest、`sw.js`、favicon）、`/health`、`/login`、`/api/auth/*`。`/share/*`、`/plugins/*`、`/extensions/*` 等非 `/api` 的功能路徑也會 503。路徑同時以原樣（casefold）與正規化後比對，兩者都要在白名單內；封鎖則任一形式符合即擋。
- **`extensions` 功能**另外涵蓋非 `/api` 的 `/extensions`、`/plugins`、`/dashboard-plugins`。
- **T1**：四處引用中改了兩處（`_sanitizeTabPanelList`、`_applyTabVisibility`）；`_availableSidebarPanels` 不改（settings 不進可排序清單），`_toggleTabVisibilityChip` 由 P1 處理。
- **T2**：anchor 改為 inline script 的 `var p=h?clean(JSON.parse(h)):[];`，後面加上 localStorage `horo-policy` 快取的 `hide_tabs`。`fixed` 集合不改，否則 tab 排序會把 settings 當成可移動的分頁。
- **G1（新增）**：`switchPanel` 開頭，policy 隱藏的分頁一律拒絕。rail、Cmd/Ctrl+,、`toggleSettings`、quota chip、`?panel=settings` 都經過 `switchPanel`，browser smoke 逐一驗證。
- **P5**：`horo_policy.js` 以非 defer 的 `<script>` 放在 `<head>`（`horo_policy.css` 旁），保證在任何 defer script 發出請求前就掛上 `fetch` 攔截。前端透過攔截 GET `/api/settings` 的回應取得 `horo_policy`（在呼叫端拿到回應前更新），任何帶 `horo_policy_error` 的 503 都會蓋上說明畫面。
- **鎖住的 chip**：加 `chip-locked`、`aria-disabled="true"`、title「由管理員設定」，樣式為虛線框加 🔒。
- **測試如何滿足權限檢查（第 10 節第 4 點）**：
  - 單元測試（`overlay/tests/test_policy.py`）以 mock 決定 `os.geteuid` 與 `os.access` 對 policy 檔與資料夾的結果，其餘照真實系統；另有一個不 mock 的測試，確認自己寫的檔案被拒。
  - `verify_wheel.py`、`browser_smoke.py` 透過 `overlay/policy_fixture.py` 的 launcher 啟動 server：權限檢查**只對 fixture 那一個路徑**放行，內容驗證、mtime 重讀與其他路徑都走真實邏輯。`verify_wheel.py` 另外用一般方式（無例外）啟動第二個 server，確認沒有 policy 時 503、使用者自己擁有的 policy 被拒。產品本身沒有任何略過權限檢查的開關。
- **`block_features` 第一批**：八項（kanban、tasks、memory、insights、logs、terminal、mcp、extensions）全部保留。browser smoke 讓八項同時封鎖，聊天、session、workspace、mind map 的既有檢查全部通過，頁面也沒有未捕捉的錯誤，沒有需要拿掉的項目。
- **第 6 節「上游套用流程大致不必修改」不成立**：上游 boot 時只從 localStorage 讀 `hidden_tabs`，只在切換 profile 或打開 settings 面板時才向伺服器校正；settings 被 policy 藏起來時就永遠不會校正。`horo_policy.js` 在攔截到 GET `/api/settings` 時，用伺服器回傳的 `hidden_tabs`（已含 policy 分頁）更新 mirror 並套用。

## 12. Slash 指令（`allow_commands`）

### 規則

- **沒有 `allow_commands` 鍵**：所有指令照上游行為，已部署的 policy 檔不必改。這個預設是實作時訂的，使用者沒有選過，請確認。
- **有這個鍵**：只有列出的指令能用。其他指令一律停用，包括頁面認得、但 policy 無法列出的指令（例如 agent registry 裡 WebUI 不分派的 `/agents`），以及上游日後新增的指令。
- **名稱嚴格檢查**，比照 `hide_tabs`：必須是字串清單，每個名稱都要在 `horo_policy.COMMANDS` 裡（小寫、canonical 名稱）。別名會被拒絕並提示正確名稱：`compact`→`compress`、`use`→`skills`、`resume`→`sessions`、`reload_mcp`→`reload-mcp` 等。不認得的名稱讓整份 policy 失效（503）。
  - 選擇「拒絕」而不是「接受並忽略」的理由：和其他鍵一致；打錯字會在部署當下就看到原因，不會默默停掉一個 admin 以為有開的指令。代價是：為新版寫的 policy（列了舊版沒有的指令）放到舊版 WebUI 會 503。
- **`/help` 永遠可用**，列不列都一樣。它只列出允許的指令；停用它，使用者就無從得知哪些能用。
- **`skills` 是總開關**：涵蓋 `/skills`、`/use`、每個 `/<skill>` 和每個 skill bundle（bundle 和 skill 指令走同一套 slug 規則，同一個 dropdown，所以合併處理）。不提供個別 skill 名稱。
- **`plugins` 是總開關**：涵蓋所有 plugin 註冊的指令。plugin 名稱是執行期才知道的，policy 無法嚴格檢查，所以不逐一列名。
- **build 檢查**：overlay 的 postcondition 從 `static/commands.js` 讀出 `COMMANDS` 與 `_WEBUI_DISPATCHABLE_AGENT_COMMANDS`，任何不在 `horo_policy.COMMANDS`（含別名）的名稱都會讓 build 失敗。新指令在執行期本來就被白名單擋下；這個檢查是確保 admin 有辦法開放它。

### `hide_composer_*` 是否連帶停用指令

實作：**policy 強制 `hide_composer_yolo: true` 時，`/yolo` 一律停用**，就算 `allow_commands` 列了 `yolo` 或沒有這個鍵。這補上了原本的缺口：藏了 YOLO 按鈕，打 `/yolo` 仍能開 YOLO。使用者自己在設定裡藏 chip 不影響。

建議但未實作：`hide_composer_model`→`/model`、`hide_composer_workspace`→`/workspace`、`hide_composer_reasoning`→`/reasoning`、`hide_composer_voice_mode`／`hide_composer_mic`→`/voice`。這幾個 chip 可能只是為了版面簡潔而藏，連帶停用指令會改變既有 policy 的效果；admin 要停用它們可以用 `allow_commands`。

### 在哪裡擋

**伺服器端（403，`horo_policy_blocked_command`）**，只擋指令專用、或由 body 可判斷的請求：

| 指令 | 拒絕的請求 |
|---|---|
| `reload-mcp` `reload-skills` `codex-runtime` `credits` | POST `/api/commands/exec`，依 `command` 的名稱（含 `_` 別名） |
| `plugins` | POST `/api/commands/exec`，名稱不是上面四個 agent 指令 |
| `skills` | `/api/commands/bundles/resolve` |
| `yolo` | POST `/api/session/yolo` 且 `enabled` 不是 false（關掉 YOLO 一律允許）；POST `/api/approval/respond` 且 `yolo: true`（approval 卡片的「略過全部」） |
| `moa` | `/api/commands/moa/resolve`；POST `/api/chat/start`、`/api/chat` 帶 `moa_config` |
| `btw` | `/api/btw` |
| `background` | `/api/background`（不含 `/api/background/status`） |
| `goal` | `/api/goal` |
| `compress` | `/api/session/compress`、`/api/session/compress/start`（不含 `/status`） |
| `retry` | `/api/session/retry` |

路徑以 `refusal()` 處理（任何 method、casefold 與正規化後的形式），body 相關的由 `command_refusal(path, body)` 處理，掛在 `handle_post` 讀完 body、通過 session 可見性檢查之後（anchor K1）。policy 無法使用時同樣回 503。

**只在前端擋**（API 和一般介面共用，伺服器端擋了會弄壞聊天）：`clear` `new` `model` `workspace` `personality` `reasoning` `theme` `usage` `status` `voice` `title` `stop` `steer` `interrupt` `queue` `undo` `branch` `sessions` `pet` `terminal`，以及 `skills` 的 `/skills`、`/use`、`/<skill>`（`/api/skills` 是 skills 分頁要用的；`/<skill>` 在 WebUI 只是送給 agent 的純文字，沒有伺服器端展開）。`terminal` 的 API 由 `block_features: terminal` 擋。注意：停用 `/undo`、`/branch` 不會拿掉訊息上的「復原」「分支」按鈕。

### 前端

- `getMatchingCommands`（K2）改名為 `_horoUpstreamGetMatchingCommands`，新的同名函式過濾掉停用的指令。來源標記決定 policy 名稱：`skill`、`bundle`→`skills`，`plugin`→`plugins`，其他用名稱加別名對照。
- `send()`（K4）在任何指令執行前、也在 busy 分支之前呼叫 `_horoRefuseBlockedCommand(text)`：認得的指令（內建、agent registry 含別名、plugin，以及 `skills` 被停用時的 skill 與 bundle）若被停用，就在本地加上使用者訊息與「此指令已被管理員停用：`/名稱`」，清空輸入框，不送給 agent。認不得的（例如 `/etc/hosts`）照常送出。`literalSlash` 送出（以純文字送出）不受影響。
- 前端還沒有 policy（或快取是舊格式）時，先 GET `/api/settings`；仍然拿不到就拒絕認得的指令。
- `/help`（K3）只列允許的內建指令。
- `/yolo` 停用時，`<html>` 加上 `horo-no-yolo`，approval 卡片的「略過全部」（`#approvalSkipAll`）隱藏。

### 測試

- `test_policy.py` 的 `Commands`：鍵不存在、驗證（別名、大小寫、非清單）、白名單、指令專用 endpoint 與共用 endpoint、`/api/commands/exec`、YOLO 只能關不能開、`hide_composer_yolo` 連帶停用、`moa_config`、沒有 policy、client view。
- `verify_wheel.py`：真 server 上，被停用指令的 endpoint 回 403 並帶指令名；允許的指令與共用 endpoint 不受影響。
- `browser_smoke.py`：dropdown 只剩允許的指令；被停用的內建指令、skill、bundle、別名在一般與 busy 狀態下都顯示管理員訊息，不發出任何 chat 或指令請求；`/help` 只列允許的；`/etc/hosts` 等照常送出；`#approvalSkipAll` 隱藏。
