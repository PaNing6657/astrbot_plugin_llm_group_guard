"""WebUI 移动端适配静态自检（无需浏览器）

校验 pages/guard 下的 HTML/CSS/JS 三件套是否仍然满足移动端适配约束，
避免后续改动把窄屏布局改回"横向溢出 / 不可点 / 输入框自动放大"的状态。

运行：python tests/test_mobile_ui_static_check.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = ROOT / "pages" / "guard"
css = (BASE / "style.css").read_text(encoding="utf-8")
html = (BASE / "index.html").read_text(encoding="utf-8")
js = (BASE / "app.js").read_text(encoding="utf-8")

problems = []
checks = []


def check(name, ok):
    checks.append(("OK  " if ok else "FAIL") + " " + name)
    if not ok:
        problems.append(name)


# 1. CSS 结构完整性
stripped = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
check("CSS 花括号平衡", stripped.count("{") == stripped.count("}"))
check("CSS 注释闭合", css.count("/*") == css.count("*/"))

# 2. 自定义属性：使用前必须已定义（--tg-* 由 .toggle 局部定义）
defined = set(re.findall(r"(--[\w-]+)\s*:", css))
used = set(re.findall(r"var\((--[\w-]+)", css))
check("CSS 变量均已定义", not {v for v in used - defined if not v.startswith("--tg-")})

# 3. 非法声明 / 空规则
bad = []
for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", stripped):
    if not m.group(2).strip():
        bad.append("空规则 " + m.group(1).strip()[:40])
    for decl in m.group(2).split(";"):
        decl = decl.strip()
        if decl and ":" not in decl:
            bad.append(f"非法声明 `{decl}` @ {m.group(1).strip()[:40]}")
check("无非法 CSS 声明", not bad)

# 4. 固定宽度不得撑破窄屏（>320px 必须有 max-width 兜底）
wide = []
for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", stripped):
    sel, body = m.group(1).strip(), m.group(2)
    w = re.search(r"(?<!max-)(?<!min-)\bwidth:\s*(\d+)px", body)
    if w and int(w.group(1)) > 320 and "max-width" not in body:
        wide.append(f"{sel[:50]} → {w.group(1)}px")
check("宽元素都有 max-width 兜底", not wide)

# 5. HTML 结构
ids = re.findall(r'\bid="([^"]+)"', html)
check("HTML id 唯一", len(ids) == len(set(ids)))
check("viewport 声明存在", "width=device-width" in html)
check("viewport 不锁定缩放", "maximum-scale" not in html and "user-scalable=no" not in html)
check("定时禁言表单有 id（JS 依赖）", 'id="schedule-form"' in html)

# 6. JS ↔ HTML 契约：$("id") 必须存在
js_ids = set(re.findall(r'\$\("([^"]+)"\)', js))
dynamic = {"retryGroups"}  # 由 openGroupPicker / renderGroupError 动态生成
check("JS 引用的 id 都存在", not (js_ids - set(ids) - dynamic))

# 7. JS 使用的类名必须在 CSS 有定义（含状态类）
js_classes = set()
for m in re.finditer(r'querySelector(?:All)?\("([^"]+)"\)', js):
    js_classes.update(re.findall(r"\.([\w-]+)", m.group(1)))
missing = {c for c in js_classes if f".{c}" not in css}
for c in ("show", "active", "on", "locked", "hidden", "weekly", "err",
          "overflowing", "at-start", "at-end", "ok", "bad"):
    if re.search(r"classList\.(?:add|toggle|remove)\(\s*[\"']" + c + r"[\"']", js) and f".{c}" not in css:
        missing.add(c)
check("JS 使用的类名都已定义", not missing)

# 8. 移动端适配要点
def mobile_block():
    m = re.search(r"@media \(max-width: 768px\)\s*\{([\s\S]*)\n\}", css)
    return m.group(1) if m else ""


def rule_tracks(block, selector):
    m = re.search(re.escape(selector) + r"\s*\{[^}]*grid-template-columns:\s*([^;]+);", block)
    return m.group(1).strip() if m else ""


# 单列 = 只有一个轨道（1fr / minmax(0,1fr)）；用轨道数判断，避免绑定具体写法
mobile = mobile_block()
check("窄屏媒体查询存在", bool(mobile))
for sel in (".form-grid", ".schedule-form"):
    tracks = rule_tracks(mobile, sel)
    # 轨道数：minmax(...) 计 1，其余按空白切分
    count = len(re.findall(r"minmax\([^)]*\)", tracks)) + len(
        [t for t in re.sub(r"minmax\([^)]*\)", " ", tracks).split() if t]
    )
    check(f"窄屏 {sel} 轨道数 ≤ 2（可堆叠）", bool(tracks) and count <= 2)
check("窄屏输入 16px 防 iOS 自动放大", "font-size: 16px" in mobile)
check("表格横滑容器", ".table-wrap {" in css and "overflow-x: auto" in css)
check("tab 窄屏单行横滑", re.search(r"\.tabs\s*\{[\s\S]*?overflow-x: auto", mobile) is not None)
check("配置网格列数由 CSS 控制（无内联覆盖）", 'style="grid-template-columns' not in js)
check("触屏热区规则", "@media (hover: none)" in css)
check("弹窗动态视口高度", "dvh" in css)
check("安全区内边距", "env(safe-area-inset" in css)
check("减弱动效支持", "@media (prefers-reduced-motion: reduce)" in css)
check("页面横向溢出防护", "overflow-x: hidden" in css)
check("tab 切换自动定位", "revealTab" in js)
check("表格横滑提示 JS+CSS 成对", "overflowing" in js and ".table-wrap.overflowing" in css)

print("=== WebUI 移动端静态检查 ===")
for c in checks:
    print(" ", c)
if problems:
    print("\n未通过：")
    for p in problems:
        print("  -", p)
    sys.exit(1)
print("\n全部通过")
