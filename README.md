# g700 Patch 套用流程指南

這份文件記錄如何從 vendor 提供的 `alps_pre/`、`alps/` 差異生成 `diff_git.patch`，並在 Android repo (`~/g700`) 內套用與自動 commit。  
同流程適用於後續 P-tag（如 P28、P29），僅需替換目錄與 P-tag 名稱。

---

## 0. 目錄與環境假設

假設 Android 專案根目錄如下：
```bash
~/g700
```

假設 Vendor 提供的 patch 目錄如下：
```bash
~/g700_patch/ALPSXXXXXXXX(..._PXX)/
```

內容包含(可將script放在這邊)：
```
alps_pre/
alps/
patch_list.txt
auto_commit_from_patchlist.py
```

---

## 1. 產生含 binary 的 diff_git.patch

```bash
cd ~/g700_patch/ALPSXXXXXXXX(..._PXX)
git diff --no-index --binary ./alps_pre ./alps > diff_git.patch
```

說明：
- `--no-index`：允許比較非 git repo 的兩個資料夾  
- `--binary`：包含二進位檔案差異  
- 之後 apply 時需加 `-p3`

---

## 2. 在 repo 套用 patch

回到 Android 根目錄：
```bash
cd ~/g700
PATCH_FILE="$HOME/g700_patch/ALPSXXXXXXXX(..._PXX)/diff_git.patch"
```

檢查與套用流程：

```bash
# 檢查變動概要（不實際修改）
git apply --stat -p3 "$PATCH_FILE"

# 確認沒有 conflict
git apply --check -p3 "$PATCH_FILE"

# 實際套用 patch（可加 --whitespace=fix）
git apply -p3 "$PATCH_FILE"
```

---

## 3. 自動化 commit

### 3.1 檢查 commit 計畫

```bash
cd ~/g700_patch/ALPSXXXXXXXX(..._PXX)
python3 auto_commit_from_patchlist.py \
  --root ~/g700 \
  --p-tag PXX \
  --dry-run
```

輸出內容包括：
- 專案清單與 CR 對應
- 生成的 commit 計畫與標題
- 每個 commit 涉及的檔案

確認分組與標題格式皆正確後進行正式 commit。

### 3.2 正式 auto commit

```bash
python3 auto_commit_from_patchlist.py \
  --root ~/g700 \
  --p-tag PXX
```

腳本會自動根據 patch_list：
- 找出每個檔案所屬 repo
- 自動 `git add` + `git commit`
- 自動產生標題與內容格式

## 指令速查表

| 動作 | 指令 |
|------|------|
| 產生含 binary 的 patch（在 vendor 資料夾） | `git diff --no-index --binary ./alps_pre ./alps > diff_git.patch` |
| 檢查 patch 涉及檔案（檢視 summary） | `git apply --stat -p3 diff_git.patch` |
| 模擬套用（不更動檔案，檢查是否可套用） | `git apply --check -p3 diff_git.patch` |
| 實際套用 patch（保留路徑層級） | `git apply -p3 diff_git.patch` |
| 使用腳本預覽 commit 計畫（dry-run） | `python3 auto_commit_from_patchlist.py --root ~/g700 --p-tag PXX --dry-run` |
| 使用腳本自動產生並提交 commit（正式執行） | `python3 auto_commit_from_patchlist.py --root ~/g700 --p-tag PXX` |
| 顯示含特定 P-tag 的 commit（範例 P27） | `repo forall -c 'git log --oneline --grep "^\[P27\]"'` |

備註：
- 將 `PXX` 替換為實際的 P-tag（如 `P28`、`P29`）。
- `--root` 與 `PATCH_FILE` 的路徑請依實際環境調整（例如使用絕對路徑或 `$HOME/g700_patch/...`）。
- 建議先執行 `--dry-run` 以確認 commit 分組與標題，再執行正式指令。
- 後續git push需手動進行或另外寫腳本，這部分比較簡單。