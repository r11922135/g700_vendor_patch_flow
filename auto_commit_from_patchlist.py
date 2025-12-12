#!/usr/bin/env python3
import argparse
import os
import re
import subprocess
import sys
import tempfile
from collections import defaultdict, deque

# ------------------------------------------------------------
# 1. 解析 patch_list.txt
# ------------------------------------------------------------

def parse_patch_list(path):
    """
    回傳一個 list，每個 element 是：
    {
        "cr_id": "ALPS10624524",
        "patch_type": "Customer Request" 或 "",
        "severity": "<字串或空字串>",
        "description_first": "<Description 第一行（原始）>",
        "description_full": "<Description 區塊全文（可含多行）>",
        "files": ["vendor/...", "frameworks/..."]
    }
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"patch_list.txt not found at {path}")

    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()

    # 以 "Patch Type:" 切每筆
    raw_entries = content.split("Patch Type:")
    entries = []
    for raw in raw_entries:
        if raw.strip():
            # 把前面的 "Patch Type:" 加回去，方便解析
            if not raw.lstrip().startswith(":"):
                raw = "Patch Type:" + raw
            entries.append(raw)

    results = []
    for entry in entries:
        lines = entry.splitlines()
        if not lines:
            continue

        cr_id = None
        patch_type = ""
        severity = ""
        desc_first = "No Description"
        desc_full_lines = []
        files = []

        i = 0
        while i < len(lines):
            line = lines[i]
            s = line.strip()

            # Patch Type:
            if s.startswith("Patch Type"):
                # 格式通常:
                # Patch Type:
                #   Customer Request
                j = i + 1
                val = ""
                while j < len(lines) and not lines[j].strip():
                    j += 1
                if j < len(lines):
                    val = lines[j].strip()
                patch_type = val
                i += 1
                continue

            # CR ID:
            if s.startswith("CR ID"):
                # 支援 "CR ID: xxx" 與下一行才是值兩種
                m = re.match(r"CR ID:\s*(\S.*)?", s)
                if m and m.group(1):
                    cr_id = m.group(1).strip()
                else:
                    j = i + 1
                    while j < len(lines) and not lines[j].strip():
                        j += 1
                    if j < len(lines):
                        cr_id = lines[j].strip()
                i += 1
                continue

            # Severity:
            if s.startswith("Severity"):
                j = i + 1
                val = ""
                # 有可能下一行就是空白代表沒填
                while j < len(lines) and not lines[j].strip():
                    # 這邊保留第一個空白當「空 severity」
                    # 所以直接 break 就好
                    break
                if j < len(lines) and lines[j].strip():
                    val = lines[j].strip()
                severity = val
                i += 1
                continue

            # Description 區塊
            if s.startswith("Description"):
                j = i + 1
                desc_lines = []
                while j < len(lines):
                    next_line = lines[j]
                    if next_line.strip().startswith("Associated Files"):
                        break
                    desc_lines.append(next_line.rstrip("\n"))
                    j += 1
                desc_full_lines = [l.rstrip() for l in desc_lines]
                for l in desc_lines:
                    if l.strip():
                        desc_first = l.strip()
                        break
                i = j
                continue

            # Associated Files 後面全部視為檔案路徑（非空行）
            if s.startswith("Associated Files"):
                j = i + 1
                while j < len(lines):
                    l = lines[j].strip()
                    if l:
                        files.append(l)
                    j += 1
                break

            i += 1

        if not cr_id:
            continue

        result = {
            "cr_id": cr_id,
            "patch_type": patch_type,
            "severity": severity,
            "description_first": desc_first,
            "description_full": "\n".join(desc_full_lines).strip(),
            "files": files,
        }
        results.append(result)

    return results


# ------------------------------------------------------------
# 2. 檔案 -> git project (往上找 .git)
# ------------------------------------------------------------

def find_git_project_for_file(root_dir, file_rel_path, cache):
    """
    給一個相對於 root_dir 的檔案路徑（例如 vendor/...），
    一路往上找 .git，找到的那一層當作 project root。

    回傳：
      - project 路徑（相對於 root_dir，例如 "frameworks/base"）
      - 找不到就回傳 None
    """
    root_dir_abs = os.path.abspath(root_dir)
    abs_path = os.path.abspath(os.path.join(root_dir_abs, file_rel_path))

    # 不在 root 底下就忽略
    if not abs_path.startswith(root_dir_abs):
        print(f"[WARN] File '{file_rel_path}' is not under root '{root_dir_abs}', skip.")
        return None

    # 從檔案所在的 dir 開始往上找
    cur = os.path.dirname(abs_path)

    while True:
        if cur in cache:
            proj_root = cache[cur]
            break

        git_marker = os.path.join(cur, ".git")
        if os.path.exists(git_marker):
            cache[cur] = cur
            proj_root = cur
            break

        # 到 root 了還沒找到 .git，視為沒有 project
        if os.path.abspath(cur) == root_dir_abs:
            cache[cur] = None
            proj_root = None
            break

        parent = os.path.dirname(cur)
        if parent == cur:
            cache[cur] = None
            proj_root = None
            break

        cur = parent

    if proj_root is None:
        return None

    project_rel = os.path.relpath(proj_root, root_dir_abs)
    # root 本身是 git repo 的情況：relpath 會是 "."
    if project_rel == ".":
        project_rel = ""  # 用空字串代表根 repo
    return project_rel


def build_project_groups(parsed, root_dir):
    """
    parsed: parse_patch_list() 的結果
    root_dir: 專案根目錄（~/g700）

    回傳：
    - project_map: { project_path: { cr_id: set(files_in_this_project) } }
        project_path 例如 "frameworks/base" 或 "" (root repo)
    - cr_info: { cr_id: {... 各種欄位 ...} }
    """
    project_map = defaultdict(lambda: defaultdict(set))
    cr_info = {}
    git_cache = {}  # dir_abs -> project_root_abs 或 None

    for item in parsed:
        cr_id = item["cr_id"]
        patch_type = item["patch_type"]
        severity = item["severity"]
        desc_first = item["description_first"]
        desc_full = item["description_full"]
        files = item["files"]

        # 記錄 CR 的描述與欄位
        if cr_id not in cr_info:
            cr_info[cr_id] = {
                "patch_type": patch_type,
                "severity": severity,
                "description_first": desc_first,
                "description_full": desc_full,
            }

        for f in files:
            proj = find_git_project_for_file(root_dir, f, git_cache)
            if proj is None:
                print(f"[WARN] No git project found for file '{f}' (CR {cr_id})")
                continue
            project_map[proj][cr_id].add(f)

    return project_map, cr_info


# ------------------------------------------------------------
# 3. 依 project + CR overlap 做 grouping
# ------------------------------------------------------------

def find_cr_components_per_project(project_map):
    """
    對每個 project，找出 CR 的 connected components（有共同檔案就互相相連）。

    回傳 commit_plans（尚未帶上 repo index）：

    commit_plans: list of {
        "project": "frameworks/base" 或 "",
        "group_crs": ["ALPS1", "ALPS2", ...],
        "all_files": [檔案完整路徑列表（相對於 root_dir）],
        "cr_files": { cr_id: [該 CR 在此 project 中的檔案] },
    }
    """
    commit_plans = []

    for proj, cr_files in project_map.items():
        # cr_files: { cr_id: set(files) }
        # file -> [cr_ids]
        file_to_crs = defaultdict(list)
        for cr_id, files in cr_files.items():
            for f in files:
                file_to_crs[f].append(cr_id)

        # adjacency list
        cr_ids = sorted(cr_files.keys())
        adj = {cr: set() for cr in cr_ids}
        for f, crs in file_to_crs.items():
            if len(crs) > 1:
                for i in range(len(crs)):
                    for j in range(i + 1, len(crs)):
                        a, b = crs[i], crs[j]
                        adj[a].add(b)
                        adj[b].add(a)

        # DFS / BFS 找連通元件
        visited = set()
        for cr in cr_ids:
            if cr in visited:
                continue
            component = []
            dq = deque([cr])
            visited.add(cr)
            while dq:
                node = dq.popleft()
                component.append(node)
                for nb in adj[node]:
                    if nb not in visited:
                        visited.add(nb)
                        dq.append(nb)
            component_sorted = sorted(component)

            # 收集此 group 的所有檔案（此 project 中）
            all_files = set()
            cr_files_subset = {}
            for c in component_sorted:
                files = sorted(cr_files[c])
                cr_files_subset[c] = files
                all_files.update(files)

            commit_plans.append({
                "project": proj,  # 可能是 "" (root repo)
                "group_crs": component_sorted,
                "all_files": sorted(all_files),
                "cr_files": cr_files_subset,
            })

    return commit_plans


# ------------------------------------------------------------
# 4. 計算 [i/n]（同一組 CR 出現在多個 repo）
# ------------------------------------------------------------

def assign_repo_index(commit_plans):
    """
    依照 group_crs (CR ID 的組合) 分組。
    同一組 CR 在不同 project 出現 -> 給 [i/n]。
    """
    groups = defaultdict(list)  # key: tuple(group_crs) -> list of indices

    for idx, plan in enumerate(commit_plans):
        key = tuple(plan["group_crs"])
        groups[key].append(idx)

    for key, idxs in groups.items():
        # 按 project 名稱排序，讓順序穩定
        idxs.sort(key=lambda i: commit_plans[i]["project"])
        total = len(idxs)
        for pos, i in enumerate(idxs, start=1):
            commit_plans[i]["repo_index"] = pos
            commit_plans[i]["repo_total"] = total


# ------------------------------------------------------------
# 5. Commit message 組合
# ------------------------------------------------------------

def transform_description_for_title(desc_first):
    """
    Description 第一行轉成 commit title 用的主題行。

    特例：
      [Google Security Patch][CVE-XXXX]Something
    -> [Google Security Patch] Something
    """
    if not desc_first:
        return ""
    m = re.match(r"\[Google Security Patch\]\s*\[[^\]]+\](.*)", desc_first)
    if m:
        return "[Google Security Patch] " + m.group(1).strip()
    return desc_first


def build_commit_title(p_tag, group_crs, cr_info, repo_index=None, repo_total=None):
    """
    產生 commit 第一行：
      [P27][ALPS1][ALPS2] Something [1/2]
    說明文字使用 group_crs 裡「排序後第一個 CR」的 Description 第一行。
    """
    if not group_crs:
        return f"[{p_tag}]"

    first_cr = sorted(group_crs)[0]
    desc_first = cr_info.get(first_cr, {}).get("description_first", "")
    subject_core = transform_description_for_title(desc_first)

    parts = [f"[{p_tag}]"]
    for cr in group_crs:
        parts.append(f"[{cr}]")
    title = "".join(parts)
    if subject_core:
        title += " " + subject_core

    if repo_index is not None and repo_total and repo_total > 1:
        title += f" [{repo_index}/{repo_total}]"

    return title


def build_commit_body(group_crs, cr_info, plan):
    """
    組成 commit body：
    每個 CR 一個區塊，格式為：

    Patch Type:
      ...
    CR ID:
      ...
    Severity:
      ...
    Description:
      ... (全文，每行縮排兩格)

    Associated Files (this project):
      ... (此 project 下屬於該 CR 的檔案)
    """
    lines = []
    for cr in group_crs:
        info = cr_info.get(cr, {})
        patch_type = info.get("patch_type", "")
        severity = info.get("severity", "")
        desc_first = info.get("description_first", "")
        desc_full = info.get("description_full", "")

        lines.append("Patch Type:")
        lines.append(f"  {patch_type}")
        lines.append("CR ID:")
        lines.append(f"  {cr}")
        lines.append("Severity:")
        lines.append(f"  {severity}")
        lines.append("")
        lines.append("Description:")
        if desc_full:
            for l in desc_full.splitlines():
                lines.append("  " + l.lstrip())
        elif desc_first:
            lines.append(f"  {desc_first}")
        else:
            lines.append("  (no description)")
        lines.append("")

        proj_files = plan["cr_files"].get(cr, [])
        if proj_files:
            lines.append("Associated Files (this project):")
            proj_root = plan["project"]
            for f in sorted(proj_files):
                if proj_root:
                    rel = os.path.relpath(f, proj_root)
                else:
                    rel = f
                lines.append(f"  {rel}")
        lines.append("")

    body = "\n".join(lines).rstrip() + "\n"
    return body


# ------------------------------------------------------------
# 6. 執行 git add / commit（或 dry-run 印計畫）
# ------------------------------------------------------------

def run_git_cmd(args, cwd, check=True, capture_output=False):
    """
    小幫手：執行 git 指令。
    """
    result = subprocess.run(
        args,
        cwd=cwd,
        check=False,
        text=True,
        stdout=subprocess.PIPE if capture_output else None,
        stderr=subprocess.PIPE if capture_output else None,
    )
    if check and result.returncode != 0:
        msg = f"Command failed (cwd={cwd}): {' '.join(args)}"
        if capture_output:
            msg += f"\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        raise RuntimeError(msg)
    return result


def perform_commits(root_dir, p_tag, commit_plans, cr_info, dry_run=False):
    """
    實際執行（或 dry-run）每個 project + CR group 的 commit。
    """
    assign_repo_index(commit_plans)

    print("========== Commit Plan ==========")
    for idx, plan in enumerate(commit_plans, start=1):
        proj = plan["project"] or "(root)"
        group_crs = plan["group_crs"]
        repo_index = plan.get("repo_index")
        repo_total = plan.get("repo_total")
        title = build_commit_title(p_tag, group_crs, cr_info, repo_index, repo_total)

        print(f"\n[{idx}] Project: {proj}")
        print(f"    CRs: {', '.join(group_crs)}")
        if repo_total and repo_total > 1:
            print(f"    Repo index: {repo_index}/{repo_total}")
        print(f"    Title: {title}")
        print("    Files:")
        for f in plan["all_files"]:
            print(f"      - {f}")

    if dry_run:
        print("\n(dry-run) 不會執行任何 git add / git commit。")
        print("\n以下是每個 commit 的完整訊息預覽：\n")

        for idx, plan in enumerate(commit_plans, start=1):
            proj = plan["project"] or "(root)"
            group_crs = plan["group_crs"]
            repo_index = plan.get("repo_index")
            repo_total = plan.get("repo_total")

            title = build_commit_title(p_tag, group_crs, cr_info, repo_index, repo_total)
            body = build_commit_body(group_crs, cr_info, plan)

            print(f"--- Commit #{idx} - Project: {proj} ---")
            print(title)
            print()
            print(body)
            print("-" * 60)
        return

    print("\n開始執行 git add / git commit ...\n")

    for idx, plan in enumerate(commit_plans, start=1):
        proj = plan["project"]  # 可能是 "" (root)
        group_crs = plan["group_crs"]
        repo_index = plan.get("repo_index")
        repo_total = plan.get("repo_total")

        repo_dir = os.path.join(root_dir, proj) if proj else root_dir
        if not os.path.isdir(repo_dir):
            print(f"[WARN] Project dir not found, skip: {repo_dir}")
            continue

        title = build_commit_title(p_tag, group_crs, cr_info, repo_index, repo_total)
        body = build_commit_body(group_crs, cr_info, plan)
        message = title + "\n\n" + body

        print(f"[{idx}] Project: {proj or '(root)'}")
        print(f"    Commit title: {title}")

        # git add
        for f in plan["all_files"]:
            abs_path = os.path.join(root_dir, f)
            if not os.path.exists(abs_path):
                print(f"    [WARN] File not found, skip add: {f}")
                continue
            rel_to_repo = os.path.relpath(abs_path, repo_dir)
            try:
                run_git_cmd(["git", "add", rel_to_repo], cwd=repo_dir, check=True)
            except RuntimeError as e:
                print(f"    [ERROR] git add failed for {rel_to_repo}: {e}")
                continue

        # 檢查是否真的有 staged changes
        diff_cached = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=repo_dir,
        )
        if diff_cached.returncode == 0:
            print("    [INFO] No staged changes, skip commit.")
            continue

        # 寫入暫存訊息檔再 commit
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as tmp:
            tmp.write(message)
            tmp_path = tmp.name

        try:
            run_git_cmd(["git", "commit", "-F", tmp_path], cwd=repo_dir, check=True)
            print("    [OK] Commit created.")
        except RuntimeError as e:
            print(f"    [ERROR] git commit failed in {repo_dir}: {e}")
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass


# ------------------------------------------------------------
# 7. main
# ------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description=(
            "根據 patch_list.txt，自動依 git project / CR 分組 "
            "並產生 git commit（支援 dry-run）。"
        )
    )
    parser.add_argument(
        "--root",
        required=True,
        help="專案根目錄（例如 ~/g700）",
    )
    parser.add_argument(
        "--patch-list",
        default="patch_list.txt",
        help="patch_list.txt 路徑（預設為當前目錄的 patch_list.txt）",
    )
    parser.add_argument(
        "--p-tag",
        required=True,
        help="P 標籤，例如 P27（會出現在 commit title 的 [P27]）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只印出計畫，不真的 git add / git commit",
    )

    args = parser.parse_args()

    root_dir = os.path.abspath(args.root)
    patch_list_path = os.path.abspath(args.patch_list)
    p_tag = args.p_tag

    print(f"專案根目錄: {root_dir}")
    print(f"patch_list: {patch_list_path}")
    print(f"P tag: {p_tag}")
    print(f"dry-run: {args.dry_run}")
    print("")

    parsed = parse_patch_list(patch_list_path)
    print(f"從 patch_list 解析出 {len(parsed)} 筆 CR 記錄。")

    project_map, cr_info = build_project_groups(parsed, root_dir)
    print(f"找出 {len(project_map)} 個 git project。")

    commit_plans = find_cr_components_per_project(project_map)
    print(f"根據 project + CR overlap 產生 {len(commit_plans)} 個 commit 計畫。")

    perform_commits(root_dir, p_tag, commit_plans, cr_info, dry_run=args.dry_run)


if __name__ == "__main__":
    main()

