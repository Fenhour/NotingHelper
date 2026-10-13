const WORD_MODE_ICON = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAADAAAAAwCAYAAABXAvmHAAAEsUlEQVR4nM2ZS4hcRRSGv37MJEQnJhl1p+LWhRuZiA8E42sviOBCYnCCCBoh+FiYGBXJSrMScZSAS5EICkrE4FYm8bEX3Kgbia8gopnpnnZR56fOranue2/3vTM5UNzue6tO/edZp6pga6gD9IBuhb5doG9jKjFum3rA0P2/BlgC7gBGDsfXwAXg0oSx20a7gEeAT4GLBOC5dtH6PA4s2NjeVoOF4AZyl2eBnygC3QDWk7aR9PkFOGQ8emyNt0Ay0YcO0BAYGNAhmwUYuj7rbtypMbxbIWl+EThrAAROWk81nbOMhBza+7PG01sWaFaijjEfAl8C9xuYOQPkJ/4Z+Bj4y/7vAR4GbnB9NEY8zgEP2DsJ3Ch4BduKMV8jWmBEyC6ngAcJQZ3SLvt22vrK7TyPz4gptjHl58APkucPwP5kXD9pnvbbmJwQzyRzNg5+PXnKfyG4wriMIl5z9n8R+NEJofar9ZnZClXAr7j+dTQmi9wO/ENMAspQjyb9alMd8ONKiA7RfXKalCXecXzlkt8Rrdk6+HHuUvZOYw8Q3Ucp+BJw1XaDXwCOWltIvvnfOwmrso+FNeC+7QLfB3YDq8R8vgrsJbiaHycff8vNo7leqwO+2wB4iH79ho3519oIeCEB7X8fzwjwsoBVAa+lfQVYJgRT3z3fAw4bePlpSn2bfBl4ycburIhhahLzvcTCbBrNS5PL5Ben87TgQlooFm2CpsCrIk3Ba06S31MHsbTxhQ2+7AC0AT51Ja0fU6VRBeydFDUu8B+1DB5iwH/gMGj+byhZyDT5MYo1+gahFrmOWD63AV7AloA/KJYSG8DBZJ6xAJTufPC8WTK4KfA3G3htdBT4vxPcZ2IxJyYHHQgBOEZckJoG77PeBYpuKx5PTBi/SYC7idEvBq8mYNsAr6wn8Eogbyf4SgU4kBHgRAK4Qwy4FLzM/i1wrfWdN/6+iZcHnyaO88DVVDz0kgD3lgjgGY0Df45qdXsK3vP4HNhn/Qrgp94QEEw+IBRmzxNqE7957wC/Eer5uwhWSk/ZFIhdQsJYMp4qSXrAU8C7rn+lzXwVC8xTrCplat+GmXeT2jB5Ljs8WbeZ1gJdwnJ+grABvwzsGNNP4CbRiGiJgT0PE4rEOYLysjSNADLv08CLNuEO4kJXlUdOo2sEy56kAviyCSDvQkcIh1A5k8/SxH+V4JqVsk0dC0ioQ8AtRLNrT/A98AnRbTyNbLxi5Qihyk15nAaeA/6mRsBOAuvXgUlBpzxfhU4Sz0tzpTk0cOomAe5hswAqrOrm+RuBM8TaZty+otYObdzE0sBt9vSmlIuU5XnxuAm4FXgMuN769Ki3Ha1NfQP6CkVNzZrn08KsyqaoFGiOlI/3jfnu+5XleX1X3aNUeYYWNA/FqnKNYqpMb1XqNl+YaRPf2KlErqqUZprI82q+MJsZfMc9ZcJlgm8OiTcuZXl+EqnvAPiKcJ0K8bxpZpIQu4HXKabK3Gakifkavazrkt8F5cDPs/lmpU5r7c73qIH9bwL4Vo8Ap6UU1IiQefqETfVDwJ806K9tkFxIpwDSfGOZok1SQI0IlwxP2vv3CRXhFa15T1Wvga44+h+5uoSKaiOTHQAAAABJRU5ErkJggg==";
const { ItemView, MarkdownView, Modal, Notice, Plugin, TFile, setIcon } = require("obsidian");
const crypto = require("crypto");
const http = require("http");
const net = require("net");
const fs = require("fs");
const path = require("path");
const { EditorState, StateEffect, StateField } = require("@codemirror/state");
const { Decoration, EditorView } = require("@codemirror/view");

const DEFAULT_PORT = 27124;
const CAPTURE_TTL_MS = 10 * 60 * 1000;
const HIGHLIGHT_SIDEBAR_TYPE = "noting-helper-highlights";
const HANDLED_EVENT = Symbol("notingHelperHandledEvent");

function highlightPipeName(token, port) {
  return String.raw`\\.\pipe\noting-helper-${crypto.createHash("sha256").update(`${token}:${port}`).digest("hex").slice(0, 20)}`;
}

function sendJson(response, status, payload) {
  // 客户端已超时并断开时，写盘仍可能成功；不能让回包错误反过来
  // 破坏这次操作，也不能再向已关闭的 socket 写第二份错误响应。
  if (response.destroyed || response.writableEnded) return;
  try {
    response.writeHead(status, {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "no-store",
    });
    response.end(JSON.stringify(payload));
  } catch (error) {
    if (!response.destroyed && !response.writableEnded) throw error;
  }
}

function readJson(request) {
  return new Promise((resolve, reject) => {
    let body = "";
    request.setEncoding("utf8");
    request.on("data", (chunk) => {
      body += chunk;
      if (body.length > 64 * 1024) {
        reject(new Error("request too large"));
        request.destroy();
      }
    });
    request.on("end", () => {
      try {
        resolve(body ? JSON.parse(body) : {});
      } catch (_error) {
        reject(new Error("invalid JSON"));
      }
    });
    request.on("error", reject);
  });
}

// 多处命中时用「离划词那一刻的偏移最近」来定。
//
// 这是 W3C Web Annotation / Hypothesis 那套锚定规范里的**标准做法**：偏移是提示、
// 原文是真相，但**有提示就要用**。用户实测撞上过：他的索引文档里同一段原文被多个
// 条目重复引用，于是正文的选区一选就有好几处候选，而这里原先直接拒绝——
// 手上有编辑器给的精确偏移却不用，等于把最有用的信息扔了。标题之所以几乎都能成，
// 正因为标题是唯一的。
//
// 仍然不猜：两处**一样近**时返回 null（真分不出来）。偏移不可用（< 0，例如 PDF
// 那条路）时也返回 null。
function pickSpan(spans, expected) {
  if (!spans.length) return null;
  if (spans.length === 1) return spans[0];
  if (!Number.isFinite(expected) || expected < 0) return null;
  const scored = spans
    .map((span) => ({ span, distance: Math.abs(span.start - expected) }))
    .sort((a, b) => a.distance - b.distance);
  if (scored[0].distance === scored[1].distance) return null;
  return scored[0].span;
}

function findSelection(content, capture) {
  // 1) 编辑器给的偏移仍然对得上 —— 最可信，重复片段也不怕
  if (capture.startOffset >= 0
      && content.slice(capture.startOffset, capture.endOffset) === capture.selection) {
    return { start: capture.startOffset, end: capture.endOffset };
  }
  // 2) 逐字找
  const exactSpans = allIndices(content, capture.selection).map(
    (at) => ({ start: at, end: at + capture.selection.length }),
  );
  if (exactSpans.length) return pickSpan(exactSpans, capture.startOffset);
  // 3) 空白容忍：编辑器里的选区可能带换行，而各处对换行的处理不一致
  const words = capture.selection.trim().split(/\s+/).filter(Boolean);
  if (!words.length) return null;
  const escaped = words.map((word) => word.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  const pattern = new RegExp(escaped.join("\\s+"), "gu");
  const looseSpans = Array.from(content.matchAll(pattern)).map(
    (match) => ({ start: match.index, end: match.index + match[0].length }),
  );
  return pickSpan(looseSpans, capture.startOffset);
}

// 阅读模式不能取 editor.getSelection()：隐藏的编辑器可能还保留上一处选区。
// 只接收当前 Markdown 视图容器内的 DOM 选区，避免拿到别的分区的文字。
function markdownViewSelection(view) {
  let selection = "";
  let startOffset = -1;
  let endOffset = -1;
  const isPreview = typeof view.getMode === "function" && view.getMode() === "preview";
  if (!isPreview && view.editor) {
    selection = view.editor.getSelection();
    if (selection.trim()) {
      startOffset = view.editor.posToOffset(view.editor.getCursor("from"));
      endOffset = view.editor.posToOffset(view.editor.getCursor("to"));
    }
  }
  if (!selection.trim()) {
    const dom = globalThis.getSelection ? globalThis.getSelection() : null;
    if (dom && view.containerEl && dom.anchorNode && dom.focusNode
        && view.containerEl.contains(dom.anchorNode)
        && view.containerEl.contains(dom.focusNode)) {
      selection = String(dom);
    }
  }
  return { selection, startOffset, endOffset };
}

function blockEndOffset(content, selectionEnd) {
  const separator = /\n[ \t]*\n/g;
  separator.lastIndex = selectionEnd;
  const match = separator.exec(content);
  return match ? match.index : content.length;
}

function existingBlockId(content, selectionStart, blockEnd) {
  const previousSeparator = content.lastIndexOf("\n\n", selectionStart);
  const blockStart = previousSeparator < 0 ? 0 : previousSeparator + 2;
  const block = content.slice(blockStart, blockEnd);
  const match = block.match(/(?:^|\s)\^([A-Za-z0-9-]+)\s*$/);
  return match ? match[1] : null;
}

// 颜色一律归一成**名字**（yellow / green / blue）。
//
// 这是踩过的一次：桌面端发过来的是十六进制色值（旧方案要把颜色写进 <mark
// style>，所以那边给的是 #e0ac00 这种），而渲染是按 `::highlight(nh-hl-yellow)`
// 注册的。直接把色值拼进名字会得到 `nh-hl-#e0ac00`——**没有对应的 CSS 规则，
// 画不出来，而且一声不吭**。PDF 与 Markdown 同时无色就是这么来的。

// 编辑器使用源文档坐标，不再在虚拟化的 DOM 中猜哪一份重复文本。
const replaceEditorMarks = StateEffect.define();
const appendEditorMark = StateEffect.define();
const editorMarks = StateField.define({
  create: () => Decoration.none,
  update(value, transaction) {
    let mapped = value.map(transaction.changes);
    for (const effect of transaction.effects) {
      if (effect.is(replaceEditorMarks)) mapped = makeEditorMarks(effect.value);
      if (effect.is(appendEditorMark)) {
        const mark = effect.value;
        if (mark.from < mark.to) {
          mapped = mapped.update({ add: [Decoration.mark({
            class: `nh-hl-${renderedColor(mark.color)}`,
            highlightId: mark.id,
            attributes: mark.id ? { "data-nh-highlight-id": mark.id } : {},
          }).range(mark.from, mark.to)], sort: true });
        }
      }
    }
    return mapped;
  },
  provide: (field) => EditorView.decorations.from(field),
});

function marksFromSet(set) {
  const marks = [];
  set.between(0, 1e12, (from, to, value) => {
    marks.push({ from, to, color: value.spec.class.slice("nh-hl-".length),
      id: value.spec.highlightId });
  });
  return marks;
}

function makeEditorMarks(marks) {
  return Decoration.set(marks.filter((mark) => mark.from < mark.to)
    .sort((a, b) => a.from - b.from || a.to - b.to)
    .map((mark) => Decoration.mark({ class: `nh-hl-${renderedColor(mark.color)}`,
      highlightId: mark.id,
      attributes: mark.id ? { "data-nh-highlight-id": mark.id } : {} })
      .range(mark.from, mark.to)), true);
}

// 只处理与旧高光内部真正相交的编辑；在高光前方插字由 CM 映射即可，
// 不应让整篇文档的高光失效。旧坐标用于匹配事务，不能先 map 再比较。
function affectedEditorMarkIds(marks, changes) {
  const ids = new Set();
  changes.iterChangedRanges((from, to) => {
    marks.between(Math.max(0, from - 1), to + 1, (start, end, value) => {
      const touches = from === to
        ? from > start && from < end
        : from < end && to > start;
      if (touches && value.spec.highlightId) ids.add(value.spec.highlightId);
    });
  });
  return ids;
}

function editorAnchorSpan(content, anchor) {
  if (anchor.kind !== "md" || !anchor.exact) return null;
  const start = Number(anchor.start);
  // 选区当时的精确位置优先；不依赖可见 DOM 或原文是否唯一。
  if (Number.isSafeInteger(start) && start >= 0
      && content.slice(start, start + anchor.exact.length) === anchor.exact) {
    return { from: start, to: start + anchor.exact.length };
  }
  const hit = resolveAnchor(content, anchor, { requireContext: true });
  if (hit) return { from: hit.start, to: hit.end };
  const candidates = allIndices(content, anchor.exact);
  if (candidates.length === 1) {
    return { from: candidates[0], to: candidates[0] + anchor.exact.length };
  }
  // 同文同上下文且已发生离线编辑时没有可靠的唯一映射，宁可不误涂。
  return null;
}


// 只有本次成功保存、当前选区仍是原选区时才收起原生紫色遮罩。
// 不能在 HTTP 等待期间清除用户新划的另一段，也不能碰剪贴板。
function clearDomSelectionInside(container, exact) {
  const selection = container?.ownerDocument?.getSelection?.();
  if (!selection || !selection.rangeCount || selection.isCollapsed
      || String(selection).trim() !== String(exact).trim()
      || !container.contains(selection.anchorNode)
      || !container.contains(selection.focusNode)) return false;
  selection.removeAllRanges();
  return true;
}

function clearEditorSelectionIfPainted(view, anchor) {
  const editor = view?.editor;
  const cm = editor?.cm;
  const marks = cm?.state?.field(editorMarks, false);
  if (!marks || !anchor || anchor.kind !== "md"
      || editor.getSelection() !== anchor.exact) return false;
  const from = editor.posToOffset(editor.getCursor("from"));
  const to = editor.posToOffset(editor.getCursor("to"));
  if (from !== anchor.start || to !== anchor.end) return false;
  let painted = false;
  marks.between(from, to, (start, end, value) => {
    if (start === from && end === to
        && value.spec.class === `nh-hl-${renderedColor(anchorColorHex(anchor))}`) painted = true;
  });
  if (!painted) return false;
  const cursor = editor.offsetToPos(to);
  editor.setSelection(cursor, cursor);
  return true;
}

function editorMarksForDocument(content, anchors) {
  return anchors.map((anchor) => {
    const span = editorAnchorSpan(content, anchor);
    return span && { ...span, color: renderedColor(anchorColorHex(anchor)),
      ...(anchor.id ? { id: anchor.id } : {}) };
  }).filter(Boolean);
}

// 高光颜色是**固定的 8 种**，不是 2.3.x 那种「可自定义色值的三个槽位」。色名、色值、
// 导出圆点三者在 HIGHLIGHT_COLOR_DEFS 里一一对应，槽位名和实际色值不可能再对不上。
// 圆点只能挑 emoji 里真实存在的圆圈（🔴🟠🟡🟢🔵🟣🩷⚫），所以这 8 组里没有「青」。
const HIGHLIGHT_COLOR_DEFS = Object.freeze({
  red: { label: "红", hex: "#e5484d", dot: "\u{1F534}" },
  orange: { label: "橙", hex: "#f76b15", dot: "\u{1F7E0}" },
  yellow: { label: "黄", hex: "#e0ac00", dot: "\u{1F7E1}" },
  green: { label: "绿", hex: "#08b94e", dot: "\u{1F7E2}" },
  blue: { label: "蓝", hex: "#086ddd", dot: "\u{1F535}" },
  purple: { label: "紫", hex: "#8e4ec6", dot: "\u{1F7E3}" },
  magenta: { label: "品红", hex: "#ff00ff", dot: "\u{1FA77}" },
  gray: { label: "灰", hex: "#8b8b8b", dot: "⚫" },
});
const HIGHLIGHT_COLORS = Object.keys(HIGHLIGHT_COLOR_DEFS);
const DEFAULT_HIGHLIGHT_PALETTE = Object.freeze(Object.fromEntries(
  HIGHLIGHT_COLORS.map((key) => [key, HIGHLIGHT_COLOR_DEFS[key].hex])));

// 8 色是**可记录的全部颜色**，悬浮窗（划选色盘）上只摆其中 3 种，由桌面端配置页选。
// 默认取 yellow/green/blue —— 正好是 2.3.x 那三个槽位的样子，升级过来观感不变。
// 历史高光不受影响：那 7 条品红照样渲染、照样进「所有高光」档，只是不能再新标品红。
const ACTIVE_COLOR_COUNT = 3;
const DEFAULT_ACTIVE_COLORS = Object.freeze(["yellow", "green", "blue"]);

// 只认 8 色表里的键，去重后按传入顺序取前 3 个；凑不满 3 个就整体回默认，不半途混搭。
function normalizeActiveColors(value) {
  const list = Array.isArray(value) ? value : [];
  const seen = [];
  for (const item of list) {
    const key = normalizeHighlightColor(item);
    if (key && !seen.includes(key)) seen.push(key);
  }
  return seen.length === ACTIVE_COLOR_COUNT ? seen : [...DEFAULT_ACTIVE_COLORS];
}

// hex → 键。8 个固定色值之外还认 2.3.x 自定义调色板留下的真实快照：当年 green 槽画的
// 是蓝（#0080ff）、blue 槽画的是绿（#00ff40），按**用户实际看到的颜色**归类。
const HIGHLIGHT_COLOR_ALIASES = (() => {
  const map = Object.create(null);
  for (const key of HIGHLIGHT_COLORS) map[HIGHLIGHT_COLOR_DEFS[key].hex] = key;
  map["#0080ff"] = "blue";
  map["#00ff40"] = "green";
  return map;
})();

function normalizeHighlightColor(value) {
  const text = String(value || "").trim().toLowerCase();
  if (HIGHLIGHT_COLORS.includes(text)) return text;
  return HIGHLIGHT_COLOR_ALIASES[text] || "";
}
function highlightColorDef(key) {
  return HIGHLIGHT_COLOR_DEFS[normalizeHighlightColor(key)] || HIGHLIGHT_COLOR_DEFS.yellow;
}

// 一条锚点归到哪个固定色。**快照优先**：colorHex 是当年真正画出来的颜色，而 color 键
// 可能是 2.3.x 那套可自定义调色板留下的、和颜色对不上的旧标签（用户那里 yellow 存着
// #ff00ff）。两边都在 8 色表里且不一致时以快照为准，于是升级后旧高光的视觉零变化、
// 不需要改写 JSONL；color 只在快照认不出时兜底。
function highlightColorKey(anchor) {
  return normalizeHighlightColor(anchor?.colorHex)
    || normalizeHighlightColor(anchor?.color) || "yellow";
}
// 渲染、导出分档、计数一律走这三个，拿到的必然是 8 个固定色值之一。少走一处，
// 同一条高光就会在编辑器、侧栏、PDF 里显示成不同颜色。
function anchorColorHex(anchor) {
  return HIGHLIGHT_COLOR_DEFS[highlightColorKey(anchor)].hex;
}
function anchorColorLabel(anchor) { return HIGHLIGHT_COLOR_DEFS[highlightColorKey(anchor)].label; }
function anchorColorDot(anchor) { return HIGHLIGHT_COLOR_DEFS[highlightColorKey(anchor)].dot; }

function renderedColor(value) {
  const text = String(value || "").toLowerCase();
  // 2.3.x 的自定义色值样式表会给旧类名留 `rgb-<hex>` 后缀，原样放行不去动它。
  if (/^rgb-[0-9a-f]{6}$/.test(text)) return text;
  return normalizeHighlightColor(text) || "yellow";
}

// 色名胶囊用高光色实心填充，文字颜色得跟着底色走，否则自定义的浅色／深色会出现
// 白底白字。按 WCAG 的相对亮度算，亮底配深字、暗底配白字。
function readableOnColor(hex) {
  const value = String(hex || "").replace(/^#/, "");
  if (!/^[0-9a-f]{6}$/i.test(value)) return "#1e1e1e";
  const channels = [0, 2, 4].map((index) => {
    const channel = parseInt(value.slice(index, index + 2), 16) / 255;
    return channel <= 0.03928 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
  });
  const luminance = 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2];
  return luminance > 0.45 ? "#1e1e1e" : "#ffffff";
}

// --------------------------------------------------------------- 卡片时间
//
// 侧栏卡片要显示「记录时间」和「上次修改注释时间」，但这两个时间**不是存储字段**：
// anchor 只在新建时存了 createdAt，注释保存只往 journal 记录上写记录级 `at`。所以
// 两者都靠重放派生（见 replayHighlightLines）：createdAt 取首次出现的时间，updatedAt
// 取最后一次「注释类字段」变化的时间。派生值随重放重算，历史记录因此立刻就有时间
// 显示，不需要迁移脚本。唯一会把它写进磁盘的是一次性迁移快照（它整对象展开 anchor），
// 写出的就是这个正确值，重放原样带过即可。

// 只有这些字段变化才算「修改注释」。改颜色、以及编辑器造成的原文漂移都不算，
// 否则用户每打一个字都会被算成修改注释。title（注释标题）是注释自己的字段，
// 改标题同样算修改注释。
const ANNOTATION_KEYS = ["note", "noteLayout", "tags", "images", "imageNames", "title"];

function touchesAnnotation(value) {
  if (!value || typeof value !== "object") return false;
  return ANNOTATION_KEYS.some((key) => key in value);
}

// 今天只给时刻，今年给月日，更早给完整年月日。缺失/无效返回空串，调用方据此整行不渲染。
function highlightTimeLabel(ms) {
  const value = Number(ms);
  if (!Number.isFinite(value) || value <= 0) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const pad = (number) => String(number).padStart(2, "0");
  const clock = `${pad(date.getHours())}:${pad(date.getMinutes())}`;
  const now = new Date();
  if (date.toDateString() === now.toDateString()) return clock;
  const monthDay = `${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
  if (date.getFullYear() === now.getFullYear()) return `${monthDay} ${clock}`;
  return `${date.getFullYear()}-${monthDay} ${clock}`;
}

// --------------------------------------------------------------- 导出命名
//
// 注释目录名 = 自定义注释标题，没设标题就用原文的前几个字。规则照搬桌面端「记录问题」
// 的命名（word_picker.question_record_slug）：取首行 → 去掉路径里不安全的符号 →
// 合并空白 → 截 48 字 → 兜底名 → Windows 保留名补后缀。
//
// 与那边唯一的不同是**不加时间戳和哈希**：记录是一次性写出的新文件，导出却要求可重复，
// 同一条高光导出两次必须落在同一个目录，否则每导出一次就换一批目录名、旧目录还得靠
// 清单回收。同名冲突改由 uniqueDirectoryName 按出现顺序补 -2、-3。
const UNSAFE_PATH_CHARS = /[[\]`*_#<>|{}\\/:"?]/g;
// 路径里也留不住首尾的空格和点（Windows 会悄悄吃掉），一并去掉。
const EDGE_PATH_CHARS = /^[ .]+|[ .]+$/g;

// 注释目录名和索引里那行标题**都从这里出**：有自定义注释标题就用它，没有就取原文的
// 前几个字。两处共用一套清洗，导出的目录名和索引标题就不会各说各话。
// 返回空串表示这条锚点既没标题也没原文，由调用方决定兜底成什么。
function annotationTitle(anchor, maxLength) {
  // 先 trim 再判空：侧栏把标题清空时存的是空串或空白，那种「有 title 字段但没内容」
  // 的情况必须回落到原文，否则会导出一个没有名字的注释目录。
  const title = String(anchor?.title ?? "").trim();
  const raw = (title || String(anchor?.exact ?? "")).replace(/\r\n?/g, "\n");
  // 标题里带换行时只取第一行，导出的目录名和标题都不该跨行。
  const first = raw.split("\n").map((line) => line.trim()).find((line) => line) || "";
  const cleaned = first.replace(UNSAFE_PATH_CHARS, " ").replace(/\s+/g, " ")
    .replace(EDGE_PATH_CHARS, "");
  return cleaned.slice(0, maxLength).replace(EDGE_PATH_CHARS, "");
}

// 导出的注释里，标签必须写成 Obsidian 认的 `#标签` 行内标签。以前是 `- 标签：a b`
// 这种纯文本，阅读视图里看着像标签，实际不进标签面板、也点不动——等于白写。
//
// 两个坑都必须躲开，否则写出来的东西仍然是纯文本：
// ① `#` 前面必须是行首或空白。`- 标签：#复习` 里 `#` 紧跟全角冒号，Obsidian 不认，
//    所以调用方在「标签：」后面留一个空格（见下面的 tagLine 用法）。
// ② 标签字符集只认字母/数字/`_`/`-`/`/`（与编辑器里提取标签用的
//    `#[\p{L}\p{N}_\-/]+` 同一套）。别的字符会让标签在那里断掉（`#a.b` 只剩 `#a`），
//    所以统一换成连字符，而不是留个看着像标签的残句。
function tagMarkup(tags) {
  return (Array.isArray(tags) ? tags : [])
    .map((tag) => String(tag ?? "").trim().replace(/^#+/, "")
      .replace(/[^\p{L}\p{N}_\-/]+/gu, "-")
      .replace(/^-+|-+$/g, ""))
    .filter(Boolean)
    .map((tag) => `#${tag}`)
    .join(" ");
}

function annotationFolderName(anchor) {
  const name = annotationTitle(anchor, 48) || "未命名注释";
  // CON、NUL 这类名字在 Windows 上做目录会直接失败（跟记录问题一样补个后缀）。
  return /^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$/i.test(name) ? `${name}-注释` : name;
}

// 同一层里重名（两份同名来源文档、两条原文相同的高光）各自落到不同目录。键统一转小写：
// Windows 和 macOS 的文件系统大小写不敏感，"A" 与 "a" 也是重名。
function uniqueDirectoryName(name, used) {
  let candidate = name;
  for (let index = 2; used.has(candidate.toLocaleLowerCase()); index += 1)
    candidate = `${name}-${index}`;
  used.add(candidate.toLocaleLowerCase());
  return candidate;
}

// 把一条记录并进用户自己维护的索引文档。块用 HTML 注释括起来：阅读视图里不可见，
// 源码里能定位，且**只以高光 ID 为键**——之后改标题只是块内容变了，位置不动。
// 纯函数，便于直接单测（CRLF 保真、重复导出不产生第二份、用户内容一律不动）。
function mergeIndexRecord(existing, id, block, eol) {
  const text = String(existing ?? "");
  const begin = `<!-- nh:begin ${id} -->`;
  const end = `<!-- nh:end ${id} -->`;
  // 块按文件既有的行尾风格写，否则同一份文档里混两种行尾，Obsidian 和 git 都会
  // 把整篇显示成变更。用户是 Windows，索引文档很可能是 CRLF。
  // 标记由这个函数自己加：调用方只给块的内容，免得它加了 begin 忘了 end，
  // 下一次导出定位不到就再追加一份。
  const inner = String(block).replace(/\n+$/, "");
  const body = eol === "\n"
    ? `${begin}\n${inner}\n${end}`
    : `${begin}${eol}${inner.replace(/\n/g, eol)}${eol}${end}`;
  const ranges = [];
  for (let at = text.indexOf(begin); at >= 0; at = text.indexOf(begin, at + 1)) {
    const stop = text.indexOf(end, at + begin.length);
    // 结束标记缺失时按「一直到文件尾」处理，不去猜别的位置。
    ranges.push([at, stop >= 0 ? stop + end.length : text.length]);
    if (stop < 0) break;
  }
  if (ranges.length) {
    // 首个块换上新内容，历史上重复导出留下的同 id 块直接删掉，保证一条高光只有一份。
    let result = "";
    let cursor = 0;
    ranges.forEach(([from, to], index) => {
      result += text.slice(cursor, from);
      if (index === 0) result += body;
      cursor = to;
    });
    return result + text.slice(cursor);
  }
  let prefix = text;
  if (prefix && !prefix.endsWith("\n")) prefix += eol;
  // 末尾只有一个换行时再补一个，让块和用户上一段之间隔开一行空行。
  if (prefix && !/(\r?\n)\s*\r?\n$/.test(prefix)) prefix += eol;
  return `${prefix}${body}${eol}`;
}

// --------------------------------------------------------------- 文本锚点
//
// 高亮**不写进正文**。旧锚点保留在 highlights.json；新锚点住在按文件日志。
// 编辑视图优先用捕获时的源文偏移；原文和前后上下文用于失效后的重定位。
// 取这套规范的要点，都是实测过的（D:/tmp/lab_reanchor.py）：
//
//   - 精确/归一化路径极快：12 万字符的笔记 + 500 处锚点，重定位合计约 38 ms
//   - **模糊匹配极慢**：2 万字符上一次 622 ms —— 所以这里根本不实现它
//   - 编辑视图 2.2.0 改用 CodeMirror Decoration.map()；下方阶梯用于失效后重定位/阅读视图
//   - **歧义时不猜**：同一片段出现多次且上下文也区分不了，就返回 null，
//     由调用方标成「待确认」。猜错位置比标不出来更糟。
const ANCHOR_CONTEXT = 64;
const FALLBACK_CONTEXT = 32;

// 归一化时折叠的字符。中文笔记里全角空格、全角破折号、中文引号都很常见，
// 不折叠就会出现「明明没改却匹配不上」。折叠出来的每个字符都要能映回源文本。
const NORMALIZE_FOLD = {
  "\u2026": "...", "\u2018": "'", "\u2019": "'",
  "\u201c": '"', "\u201d": '"', "\u2013": "-", "\u2014": "-",
  "\u3000": " ", "\u00a0": " ",
};
const NORMALIZE_BLANKS = " \t\r\n";

function describeQuote(content, start, end, context = ANCHOR_CONTEXT) {
  return {
    exact: content.slice(start, end),
    prefix: content.slice(Math.max(0, start - context), start),
    suffix: content.slice(end, end + context),
  };
}

function normalizeWithMap(text) {
  let out = "";
  const index = [];              // 归一化后的第 i 个字符 -> 源文本下标
  let i = 0;
  while (i < text.length) {
    const ch = text[i];
    if (NORMALIZE_BLANKS.includes(ch)) {          // 空白串折叠成一个空格
      let j = i;
      while (j < text.length && NORMALIZE_BLANKS.includes(text[j])) j += 1;
      out += " ";
      index.push(i);
      i = j;
      continue;
    }
    if (ch === "-" && text[i + 1] === "\n") {     // 连字符换行：整段折掉
      i += 2;
      continue;
    }
    const folded = NORMALIZE_FOLD[ch];
    if (folded) {
      for (const replacement of folded) {
        out += replacement;
        index.push(i);
      }
      i += 1;
      continue;
    }
    out += ch.toLowerCase();
    index.push(i);
    i += 1;
  }
  return { text: out, index };
}

// 比 normalizeWithMap 更宽一级：**把空白整个丢掉**，而不是收成一个空格。
//
// 这是为渲染层准备的，不是锦上添花：编辑器 DOM 里**相邻行之间没有分隔符**
// （CodeMirror 每行一个 div，textContent 拼起来不插换行），而锚点里的
// prefix/exact/suffix 取自源文件、带换行。只「收缩空白」抹不平这个差异——
// 一边有一个空格、另一边什么都没有，归一化之后仍然对不上。
//
// 实测（D:/tmp/lab_dom_match.py，93 个取样位置）：加这一级把渲染成功率从
// 51% 提到 78%。剩下的 22% 是选区里含 Markdown 标记（`[[…|X]]` 在渲染后只剩
// `X`），那类逐字匹配本质上不可能成，属于已知边界。
function normalizeLoose(text) {
  const { text: folded, index } = normalizeWithMap(text);
  let out = "";
  const kept = [];
  for (let i = 0; i < folded.length; i += 1) {
    // normalizeWithMap 已经把各类空白统一成半角空格了，所以判等就够了
    if (folded[i] === " ") continue;
    out += folded[i];
    kept.push(index[i]);
  }
  return { text: out, index: kept };
}

function allIndices(haystack, needle) {
  const hits = [];
  if (!needle) return hits;
  let at = haystack.indexOf(needle);
  while (at !== -1) {
    hits.push(at);
    at = haystack.indexOf(needle, at + 1);
  }
  return hits;
}

// 长上下文能区分重复引用，但渲染 DOM 可能缺少较远处的 Markdown 标记或行。
// 先试完整上下文，再试贴着选区的旧版 32 字符上下文；每次都要求唯一。
// 旧锚点本来只有 32 字符，不迁移也照样可读。
function quoteContexts(prefix, suffix) {
  const contexts = [{ prefix, suffix }];
  if (prefix.length > FALLBACK_CONTEXT || suffix.length > FALLBACK_CONTEXT) {
    contexts.push({
      prefix: prefix.slice(-FALLBACK_CONTEXT),
      suffix: suffix.slice(0, FALLBACK_CONTEXT),
    });
  }
  return contexts;
}

function resolveAnchor(content, anchor, { requireContext = false } = {}) {
  const exact = String((anchor && anchor.exact) || "");
  if (!exact) return null;
  const prefix = String((anchor && anchor.prefix) || "");
  const suffix = String((anchor && anchor.suffix) || "");
  const contexts = quoteContexts(prefix, suffix);

  // 1) 精确 + 上下文：上下文不同就能把重复片段分开，这是最可信的一级
  for (const context of contexts) {
    if (!context.prefix && !context.suffix) continue;
    const hits = allIndices(content, context.prefix + exact + context.suffix);
    if (hits.length === 1) {
      const start = hits[0] + context.prefix.length;
      return { start, end: start + exact.length, method: "exact-with-context" };
    }
  }

  // 2) 精确：唯一就认
  const exactHits = allIndices(content, exact);
  if (!requireContext && exactHits.length === 1) {
    return { start: exactHits[0], end: exactHits[0] + exact.length, method: "exact" };
  }

  // 3)/4) 归一化后再说（空白、全角标点、大小写的差异都能吃掉）
  const normalized = normalizeWithMap(content);
  const toSource = (pos) => {
    if (!normalized.index.length) return 0;
    const clamped = Math.max(0, Math.min(pos, normalized.index.length - 1));
    return normalized.index[clamped];
  };
  // 终点要单独算：toSource 给的是「这个字符在源里的起点」，直接当排他终点用会
  // **少一个字符**（"这里是 重点内容。" 会切出 "这里是 重点内容"）。折叠只会
  // 多对一（… 折成 ...），所以末字符的源起点 +1 就是排他终点。
  const toSourceEnd = (pos) => toSource(pos - 1) + 1;

  const normalizedExact = normalizeWithMap(exact).text;
  for (const context of contexts) {
    if (!context.prefix && !context.suffix) continue;
    // 只有**唯一**命中才算「上下文确认过」。没有上下文时这一步会退化成
    // 「取第一个」，那就是在猜——宁可交给下面判歧义。
    const normCombined = normalizeWithMap(context.prefix + exact + context.suffix).text;
    const combined = normalized.text.indexOf(normCombined);
    const unique = combined !== -1
      && normalized.text.indexOf(normCombined, combined + 1) === -1;
    if (unique) {
      const before = normalizeWithMap(context.prefix).text.length;
      const start = toSource(combined + before);
      const end = toSourceEnd(combined + before + normalizedExact.length);
      return { start, end: Math.max(end, start + 1), method: "normalized-with-context" };
    }
  }
  const hits = allIndices(normalized.text, normalizedExact);
  if (!requireContext && hits.length === 1) {
    const start = toSource(hits[0]);
    const end = toSourceEnd(hits[0] + normalizedExact.length);
    return { start, end: Math.max(end, start + 1), method: "normalized" };
  }

  // 5)/6) 连空白都容忍。渲染层与源文本最大的系统性差异就是行分隔——源文件里
  // 有换行，编辑器 DOM 里相邻行是黏在一起的。这一级专门吃这个差异。
  const loose = normalizeLoose(content);
  const toLoose = (pos) => {
    if (!loose.index.length) return 0;
    return loose.index[Math.max(0, Math.min(pos, loose.index.length - 1))];
  };
  const toLooseEnd = (pos) => toLoose(pos - 1) + 1;
  for (const context of contexts) {
    if (!context.prefix && !context.suffix) continue;
    const needle = normalizeLoose(context.prefix + exact + context.suffix).text;
    const at = loose.text.indexOf(needle);
    if (at !== -1 && loose.text.indexOf(needle, at + 1) === -1) {
      const before = normalizeLoose(context.prefix).text.length;
      const start = toLoose(at + before);
      const end = toLooseEnd(at + before + normalizeLoose(exact).text.length);
      return { start, end: Math.max(end, start + 1), method: "loose-with-context" };
    }
  }
  const looseExact = normalizeLoose(exact).text;
  const looseHits = allIndices(loose.text, looseExact);
  if (!requireContext && looseHits.length === 1) {
    const start = toLoose(looseHits[0]);
    const end = toLooseEnd(looseHits[0] + looseExact.length);
    return { start, end: Math.max(end, start + 1), method: "loose" };
  }

  // 到这儿还定不下来就是真定不下来——不模糊匹配、不猜位置
  return null;
}

// 编辑器只渲染可见行。如果整篇源文有重复，当前 DOM 的唯一 exact 可能是
// 索引后来追加的记录副本，不是当初划选的那一处。此时必须靠上下文确认。
function sourceHasRepeatedExact(sourceContent, anchor) {
  if (typeof sourceContent !== "string" || !anchor || !anchor.exact) return false;
  const at = sourceContent.indexOf(anchor.exact);
  return at !== -1 && sourceContent.indexOf(anchor.exact, at + 1) !== -1;
}

// 把落在 [from, to) 里的锚点取出来。锚点按 start 有序，所以二分即可——
// 这是「装饰只画视口内」的依据，也让开销与文档里总共有多少高光基本无关。
function anchorsInRange(sorted, from, to) {
  let lo = 0;
  let hi = sorted.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (sorted[mid].end <= from) lo = mid + 1;
    else hi = mid;
  }
  const hit = [];
  for (let i = lo; i < sorted.length && sorted[i].start < to; i += 1) hit.push(sorted[i]);
  return hit;
}

// --------------------------------------------------------- PDF 页内定位
//
// PDF 不可变，所以 PDF 的锚点只要「页码 + 页内原文 + 第几次出现」就够，
// **不需要上面那套重定位阶梯**——这是 PDF 比 Markdown 简单的地方。
//
// Obsidian 的 PDF 视图是 PDF.js：一页的文字散在 .textLayer 的多个 <span> 里，
// 一个短语常常横跨好几个。所以先把它们按 DOM 顺序拼起来，记住每个 span 的起止，
// 再在拼出来的文本里定位，最后映射回「哪个 span 的哪一段」。

// 把一串节点按顺序拼成文本，并记住每一段在拼接结果里的起止。
//
// **传进来的必须是文本节点，不能是元素节点。** 踩过：这里原来接的是 .textLayer
// 里的 <span>，于是后面 Range.setStart(span, 437) —— 而 offset 对元素节点是
// 「第几个子节点」（span 只有 1 个），对文本节点才是字符偏移，所以那些 Range
// 全是非法的，一段颜色都画不出来。同理 offsetOfNode 只在 nodeType === 3 时才认。
function readPageText(nodes) {
  let text = "";
  const parts = [];
  for (const node of nodes) {
    const value = node.nodeValue === undefined || node.nodeValue === null
      ? (node.textContent || "")
      : node.nodeValue;
    if (!value) continue;
    parts.push({ node, start: text.length, end: text.length + value.length });
    text += value;
  }
  return { text, parts };
}

// 取出一棵子树里的全部文本节点（文档顺序）
function textNodesOf(root) {
  const doc = root.ownerDocument || document;
  const walker = doc.createTreeWalker(root, 4 /* NodeFilter.SHOW_TEXT */, null);
  const nodes = [];
  let node = walker.nextNode();
  while (node) {
    nodes.push(node);
    node = walker.nextNode();
  }
  return nodes;
}

// PDF.js 的 Selection.toString() 会在视觉换行插入空白；.textLayer 的相邻
// span 却可能既没有换行也没有空格。所有捕获/重画必须共用同一套候选位置，
// 否则捕获算不出 occurrence，或重画时将重复文字画到另一处。
function pdfMatches(pageText, exact) {
  const literal = allIndices(pageText, exact);
  if (literal.length) return literal.map((start) => ({ start, end: start + exact.length }));
  for (const normalize of [normalizeWithMap, normalizeLoose]) {
    const page = normalize(pageText);
    const needle = normalize(exact).text;
    if (!needle) continue;
    const hits = allIndices(page.text, needle);
    if (hits.length) return hits.map((at) => ({
      start: page.index[at],
      end: page.index[at + needle.length - 1] + 1,
    }));
  }
  return [];
}

function locateInPage(pageText, exact, occurrence = 0) {
  const hits = pdfMatches(pageText, exact);
  if (!hits.length) return null;
  return hits[Math.min(Math.max(0, occurrence), hits.length - 1)];
}

// 页面文本里的 [start, end) -> 「哪些 span 的哪几段字符」
function slicesForRange(parts, start, end) {
  const slices = [];
  for (const part of parts) {
    const from = Math.max(start, part.start);
    const to = Math.min(end, part.end);
    if (to <= from) continue;
    slices.push({ node: part.node, from: from - part.start, to: to - part.start });
  }
  return slices;
}

// 选区起点在页面文本里的偏移；认不出端点就返回 -1（调用方按 -1 兜底）
function offsetOfNode(parts, node, offset) {
  if (!node || node.nodeType !== 3) return -1;
  for (const part of parts) {
    if (part.node === node) return part.start + offset;
  }
  return -1;
}

// 同一个短语在这一页出现多次时，用它来判定用户选的是第几个：
// 取「起点的偏移」落在哪一个命中里。判不出来就退回第 0 个。
function occurrenceForOffset(pageText, exact, offset) {
  const hits = pdfMatches(pageText, exact);
  if (!hits.length) return -1;
  if (offset < 0) return 0;
  for (let i = 0; i < hits.length; i += 1) {
    if (offset >= hits[i].start && offset < hits[i].end) return i;
  }
  return 0;
}

// 把若干 slice 变成 DOM Range。用 Range 而不是拆 span，是为了走
// CSS Custom Highlight API —— 完全不动 DOM，也就不会跟 PDF.js 的渲染打架。
function rangesForSlices(slices, createRange) {
  const makeRange = createRange || (() => document.createRange());
  const ranges = [];
  for (const slice of slices) {
    const length = (slice.node.textContent || "").length;
    if (slice.from < 0 || slice.to > length || slice.to <= slice.from) continue;
    const range = makeRange();
    range.setStart(slice.node, slice.from);
    range.setEnd(slice.node, slice.to);
    ranges.push(range);
  }
  return ranges;
}

function pageElementFromNode(node) {
  const element = node && node.nodeType === 1 ? node : node && node.parentElement;
  if (!element || typeof element.closest !== "function") return null;
  return element.closest("[data-page-number]");
}

// --------------------------------------------------------------- 浮层渲染
//
// 三处渲染都用同一批颜色；背景是**半透明**的（见 styles.css），
// 实色会让底下的黑字在暗色主题里糊掉。

function pdfPageNumbers(containerEl) {
  if (!containerEl || typeof containerEl.querySelectorAll !== "function") return [];
  return Array.from(containerEl.querySelectorAll("[data-page-number]"));
}

// CSS Custom Highlight API：给 Range 上色但**完全不改 DOM**。这一点对 PDF 尤其
// 重要——PDF.js 按自己的节奏重建文本层，我们去改它的节点会互相打架。
// 取不到就返回 null，调用方退化成不渲染（而不是去拆 DOM）。
function highlightRegistry() {
  if (typeof CSS === "undefined" || !CSS || !CSS.highlights) return null;
  if (typeof Highlight === "undefined") return null;
  return CSS.highlights;
}

function highlightRegistryName(color) {
  return `nh-hl-${color}`;
}

// 把一段 DOM 里的文本节点按顺序拼起来，并记住每个节点在拼接结果里的起止。
// 阅读视图用它——那里的文字同样散在一堆 <span>/<em>/<a> 里。
function readTextNodes(root) {
  const doc = root.ownerDocument || document;
  const walker = doc.createTreeWalker(root, 4 /* NodeFilter.SHOW_TEXT */, null);
  let text = "";
  const parts = [];
  let node = walker.nextNode();
  while (node) {
    const value = node.nodeValue || "";
    if (value) {
      parts.push({ node, start: text.length, end: text.length + value.length });
      text += value;
    }
    node = walker.nextNode();
  }
  return { text, parts };
}

// 从 PDF 视图里当前的选区取出一条锚点。**必须在划词那一刻调用**：那时选区还在，
// 等到用户点悬浮窗时选区多半已经没了。取不到就返回 null，上层如实说「没取到」。
function pdfAnchorFromSelection(containerEl) {
  const doc = containerEl && containerEl.ownerDocument;
  const selection = doc && doc.getSelection ? doc.getSelection() : null;
  if (!selection || !selection.rangeCount || selection.isCollapsed) return null;
  const exact = String(selection.toString() || "").trim();
  if (!exact) return null;
  const range = selection.getRangeAt(0);
  const pageNumber = pageNumberFromNode(range.startContainer);
  const pageEl = pageElementFromNode(range.startContainer);
  if (!pageNumber || !pageEl) return null;
  const layer = pageEl.querySelector(".textLayer");
  if (!layer) return null;
  // 必须取文本节点：Range 的偏移与 offsetOfNode 都按字符算，元素节点不认
  const { text: pageText, parts } = readPageText(textNodesOf(layer));
  const startOffset = offsetOfNode(parts, range.startContainer, range.startOffset);
  const occurrence = occurrenceForOffset(pageText, exact, startOffset);
  if (occurrence < 0) return null;
  return { page_number: pageNumber, exact, occurrence };
}

function pageNumberFromNode(node) {
  const element = node && node.nodeType === 1 ? node : node && node.parentElement;
  if (!element || typeof element.closest !== "function") return null;
  const page = element.closest("[data-page-number]");
  if (!page || typeof page.getAttribute !== "function") return null;
  const value = Number.parseInt(page.getAttribute("data-page-number"), 10);
  return Number.isInteger(value) && value > 0 ? value : null;
}

function pageNumberFromSelection(selection) {
  if (!selection || !selection.rangeCount || selection.isCollapsed) return null;
  return pageNumberFromNode(selection.anchorNode) || pageNumberFromNode(selection.focusNode);
}

function activePdfPageNumber(containerEl) {
  if (!containerEl) return null;
  const documents = [];
  const addDocument = (document) => {
    if (document && !documents.includes(document)) documents.push(document);
  };
  addDocument(containerEl.ownerDocument);
  if (typeof containerEl.querySelectorAll === "function") {
    for (const iframe of containerEl.querySelectorAll("iframe")) {
      try {
        addDocument(iframe.contentDocument);
      } catch (_error) {
        // Some third-party viewers use inaccessible frames; fall back cleanly.
      }
    }
  }
  for (const document of documents) {
    try {
      const pageNumber = pageNumberFromSelection(document.getSelection());
      if (pageNumber) return pageNumber;
    } catch (_error) {
      // A viewer update must not prevent creation of a normal file link.
    }
  }
  return null;
}

function originalLinkInfo(link) {
  if (!link || !link.classList) {
    return null;
  }
  const isRenderedLink = link.classList.contains("internal-link");
  const isEditorLink =
    link.classList.contains("cm-hmd-internal-link") ||
    link.classList.contains("cm-link");
  if (!isRenderedLink && !isEditorLink) return null;
  let href = String(
    link.getAttribute("data-href") || link.getAttribute("href") || ""
  ).trim();
  let label = String(link.textContent || "").trim();
  if (!href && typeof link.closest === "function") {
    const line = link.closest(".cm-line");
    const lineText = String(line ? line.textContent : "");
    const matches = Array.from(lineText.matchAll(/\[\[([^\]]+)\]\]/g));
    const candidate = matches.find((match) => {
      const value = match[1];
      return /(?:原文|来源|补充说明)/.test(value) || /#\^(?:[A-Za-z0-9-]+)|#page=\d+/i.test(value);
    });
    if (candidate) {
      const parts = candidate[1].split("|");
      href = String(parts.shift() || "").trim();
      label = String(parts.join("|") || href).trim();
    }
  }
  if (!href || /^https?:\/\//i.test(href)) return null;
  const hasSourceLabel = /(?:原文|来源|补充说明)/.test(label);
  const hasLocation = /#\^(?:[A-Za-z0-9-]+)|#page=\d+/i.test(href);
  if (!hasSourceLabel && !hasLocation) return null;
  return { href, label };
}

function linkElementFromTarget(target) {
  if (!target || typeof target.closest !== "function") return null;
  const link = target.closest("a.internal-link, .cm-hmd-internal-link, .cm-link");
  if (link) return link;
  const line = target.closest(".cm-line");
  if (line && /\[\[[^\]]+\]\]/.test(String(line.textContent || ""))) return line;
  return null;
}

function linkElementFromEvent(event) {
  if (!event) return null;
  const path = typeof event.composedPath === "function" ? event.composedPath() : [];
  for (const item of path) {
    if (item && item.classList && (
      item.classList.contains("internal-link") ||
      item.classList.contains("cm-hmd-internal-link") ||
      item.classList.contains("cm-link")
    )) return item;
  }
  const targetLink = linkElementFromTarget(event.target);
  if (targetLink && !targetLink.classList.contains("cm-line")) return targetLink;
  for (const item of path) {
    if (item && item.classList && item.classList.contains("cm-line") &&
        /\[\[[^\]]+\]\]/.test(String(item.textContent || ""))) return item;
  }
  return targetLink;
}

function isPlainLeftClick(event) {
  return Boolean(
    event &&
      event.button === 0 &&
      !event.ctrlKey &&
      !event.altKey &&
      !event.shiftKey &&
      !event.metaKey
  );
}

function parseWikiLinkAtOffset(text, offset) {
  const matches = Array.from(String(text || "").matchAll(/\[\[([^\]]+)\]\]/g));
  const candidate = matches.find((match) => {
    const end = match.index + match[0].length;
    const value = match[1];
    return offset >= match.index && offset <= end &&
      (/(?:原文|来源|补充说明)/.test(value) || /#\^(?:[A-Za-z0-9-]+)|#page=\d+/i.test(value));
  });
  if (!candidate) return null;
  const parts = candidate[1].split("|");
  const href = String(parts.shift() || "").trim();
  const label = String(parts.join("|") || href).trim();
  return href ? { href, label } : null;
}

function eventElementPath(event) {
  if (!event) return [];
  const path = typeof event.composedPath === "function" ? event.composedPath() : [];
  return path.length ? path : event.target ? [event.target] : [];
}

function mutationsTouchHighlightRoots(mutations, roots) {
  if (!roots.length) return false;
  for (const mutation of mutations) {
    for (const root of roots) {
      if (root.contains(mutation.target)) return true;
      for (const node of [...mutation.addedNodes, ...mutation.removedNodes]) {
        if (node === root || node.contains?.(root)) return true;
      }
    }
  }
  return false;
}

class HighlightSidebarView extends ItemView {
  constructor(leaf, plugin) {
    super(leaf);
    this.plugin = plugin;
    this.currentOnly = true;
    this.colorFilter = "all";
    this.query = "";
    // 就地展开的编辑器同时只允许有一个：换一条卡片展开前必须先收回上一条，
    // 否则旧编辑器的临时图片 URL 会一直挂着。
    this.expandedId = null;
    this.expandedDispose = null;
    // 卡片按 id 对位复用：key 记住这个节点代表哪条高光（或哪个分组标题），signature
    // 记住它是照哪一版数据画出来的。两者都对得上就原地留着，见 renderEntries。
    // 挂在节点上的 WeakMap 而不是 DOM 属性：节点被换掉时记录自己就没了。
    this.entryKeys = new WeakMap();
    this.entrySignatures = new WeakMap();
    // 当前挂在这张卡片里的就地编辑器元素。renderEntries 靠它认出「哪张卡不能复用」，
    // 比每次去整棵树里 querySelector('.nh-sidebar-editor') 便宜得多（卡片一多，那是
    // 每轮刷新最贵的一步）。收起时不清它：collapseEditor 只是回收编辑器，DOM 要等
    // 下一轮重画那张卡才换回预览框，在那之前它仍是「挂着编辑器」的证据。
    this.editorElement = null;
  }
  getViewType() { return HIGHLIGHT_SIDEBAR_TYPE; }
  getDisplayText() { return "高光索引"; }
  getIcon() { return "highlighter"; }
  async onOpen() {
    this.body = this.containerEl.children[1] || this.containerEl;
    this.render();
  }
  async onClose() {
    this.collapseEditor();
    if (this.revealTimer) clearTimeout(this.revealTimer);
    this.revealTimer = null;
  }
  // 就地展开的编辑器里有尚未保存的输入。挂着编辑器的那张卡在 renderEntries 里被排除
  // 在复用之外（收起就得靠重画它），所以后台刷新一来，编辑器连同未保存的输入一起没了；
  // 刷新循环据此跳过本视图（见 scheduleHighlightSidebarRefresh）：收起时 toggleEditor
  // 自己会 renderEntries 一次，不会漏掉期间发生的变化。
  isEditing() { return Boolean(this.expandedId); }
  // keepScroll：是否把滚动位置接回来。默认接着，只有「当前文档 / 全部文档」这种
  // 整份列表换掉、旧位置在新列表里指到别处的操作才该回到顶部（见调用点传的 false）。
  render({ keepScroll = true } = {}) {
    const body = this.body;
    if (!body) return;
    // 量位置必须在任何重建之前。
    const scroll = keepScroll ? body.scrollTop : 0;
    // 面板骨架（标题、范围开关、搜索框、卡片容器）只搭一次。以前这里是把 body 整个
    // replaceChildren 再重建：清空滚动容器的那一瞬浏览器会把 scrollTop 夹到 0，正在
    // 就地展开的编辑器也跟着丢。骨架还在时就只改它自己的文案，卡片交给 renderEntries
    // 对位复用。
    if (this.entries?.parentElement === body) {
      this.scopeButton.textContent = this.currentOnly ? "当前文档" : "全部文档";
    } else {
      this.collapseEditor();
      this.buildChrome(body);
    }
    this.renderEntries();
    // 要回顶部的一路显式写 0：别去指望「清空滚动容器时浏览器会把位置夹到 0」，
    // 那件事在无布局的环境里根本不发生（测试跑的 jsdom 就没有布局）。
    body.scrollTop = scroll;
  }

  // 从正文里点高光反向定位到这张卡：滚进视野 + 短暂描边。
  // 返回是否找到（找不到通常是那条已经被删了）。
  revealHighlight(id) {
    if (!this.entries) return false;
    // 搜索词和「只看当前文档」都可能把目标卡排除在 DOM 之外，先放开过滤。
    if (this.query) {
      this.query = "";
      if (this.searchInput) this.searchInput.value = "";
    }
    // 只有目标确实在别的文档时才关掉范围限制：用户开着「当前文档」点本文里的
    // 高光，不该被莫名切到全量列表。
    const target = this.plugin.findHighlight(id);
    if (target && this.colorFilter !== "all" && this.colorFilter !== highlightColorKey(target.anchor)) {
      this.colorFilter = "all";
    }
    if (this.currentOnly && target) {
      const active = this.plugin.app.workspace.getActiveFile?.()?.path;
      const current = active || this.plugin.lastHighlightSourcePath;
      if (current && target.filePath !== current) {
        this.currentOnly = false;
        if (this.scopeButton) this.scopeButton.textContent = "全部文档";
      }
    }
    this.render({ keepScroll: false });
    // 用 data-highlight-id 逐个比对，不拼 querySelector 字符串：id 是外部数据，
    // 拼进选择器要在特殊字符上翻车。
    let card = null;
    for (const child of this.entries.children)
      if (child.getAttribute?.("data-highlight-id") === id) { card = child; break; }
    if (!card) return false;
    // jsdom 没有布局，scrollIntoView 也可能不存在。
    if (typeof card.scrollIntoView === "function") card.scrollIntoView({ block: "center" });
    card.classList.add("is-revealed");
    if (this.revealTimer) clearTimeout(this.revealTimer);
    this.revealTimer = setTimeout(() => {
      this.revealTimer = null;
      // 期间后台刷新可能已经重画过卡片，旧节点已经不在文档里了。
      if (card.isConnected) card.classList.remove("is-revealed");
    }, 1600);
    return true;
  }

  buildChrome(body) {
    body.classList.add("nh-highlight-sidebar");
    const title = document.createElement("h3");
    title.textContent = "高光索引";
    body.appendChild(title);
    const controls = document.createElement("div");
    controls.className = "nh-sidebar-controls";
    this.scopeButton = document.createElement("button");
    this.scopeButton.type = "button";
    this.scopeButton.textContent = this.currentOnly ? "当前文档" : "全部文档";
    this.scopeButton.title = "切换当前文档 / 全部文档";
    // 换的是整份列表，旧的滚动位置在新列表里指到别处，这里明确回到顶部。
    this.scopeButton.addEventListener("click", () => {
      this.currentOnly = !this.currentOnly;
      this.render({ keepScroll: false });
    });
    controls.appendChild(this.scopeButton);
    this.colorSelect = document.createElement("select");
    this.colorOptionsSignature = null;
    this.colorSelect.className = "nh-sidebar-color-filter";
    this.colorSelect.setAttribute("aria-label", "按颜色筛选高光");
    this.colorSelect.title = "按现有高光颜色筛选";
    this.colorSelect.addEventListener("change", () => {
      this.colorFilter = this.colorSelect.value;
      this.render({ keepScroll: false });
    });
    controls.appendChild(this.colorSelect);
    const search = document.createElement("input");
    search.type = "search";
    search.placeholder = "搜索原文、注释、标签、文档名…";
    search.setAttribute("aria-label", "搜索高光");
    search.value = this.query;
    search.addEventListener("input", () => { this.query = search.value; this.renderEntries(); });
    // revealHighlight 要把搜索框一起清空（只改 this.query 的话框里还留着旧词，
    // 下一次输入会把它当起点），所以得留个引用。
    this.searchInput = search;
    controls.appendChild(search);
    body.appendChild(controls);
    this.entries = document.createElement("div");
    this.entries.className = "nh-sidebar-entries";
    body.appendChild(this.entries);
  }
  updateColorFilter(files) {
    // Use all actual records, independent of scope, search and the entry palette.
    const present = new Set();
    for (const anchors of Object.values(files))
      for (const anchor of anchors) if (anchor.id) present.add(highlightColorKey(anchor));
    const colors = HIGHLIGHT_COLORS.filter((key) => present.has(key));
    if (this.colorFilter !== "all" && !present.has(this.colorFilter)) this.colorFilter = "all";
    const signature = colors.join(",");
    if (this.colorSelect && this.colorOptionsSignature !== signature) {
      this.colorSelect.replaceChildren();
      for (const key of ["all", ...colors]) {
        const option = this.colorSelect.ownerDocument.createElement("option");
        option.value = key;
        option.textContent = key === "all" ? "全部" : HIGHLIGHT_COLOR_DEFS[key].label;
        this.colorSelect.appendChild(option);
      }
      this.colorOptionsSignature = signature;
    }
    if (this.colorSelect) this.colorSelect.value = this.colorFilter;
  }

  // 一张卡片的渲染只取决于这几个字段，全等就说明上一次画出来的 DOM 现在仍然是对的。
  // 拼成字符串比逐字段比较省事，也不会漏掉「注释正文变了但时间戳没变」这类只有内容
  // 变的情况（改锚点偏移走的是 queueAnchorEdit，不会动 updatedAt）。
  cardSignature(filePath, anchor) {
    return [
      filePath, anchor.kind || "md", anchorColorHex(anchor), anchor.exact || "",
      anchor.title || "", anchor.createdAt || 0, anchor.updatedAt || 0, anchor.page || 0,
      (anchor.tags || []).join("\u0001"), (anchor.images || []).join("\u0001"),
      (anchor.imageNames || []).join("\u0001"),
      // 注释正文那两个字段直接拼，不走 annotationLayout：后者会为没有 noteLayout 的
      // 旧记录现拼一串图片标记，等于每轮刷新给每张卡片多做一次字符串构造。
      anchor.noteLayout ?? null, anchor.note || "",
    ].join("\u0000");
  }
  // 侧栏最热的一条路径。保存、删除、桌面端新增一条都走它，而最频繁的是**在笔记里
  // 打字**：handleEditorUpdate 末尾那条 docChanged 让每个按键都在 60 ms 去抖后刷一次
  // （见 scheduleHighlightSidebarRefresh）。所以这里按 id 对位复用，只重建签名变了的
  // 那几张卡——整块 replaceChildren 除了白画一遍所有卡片，还会把滚动位置清掉、把预览
  // 里的缩略图全部重新取一遍（src 是 app:// 资源，重建就重新解码，看得见闪）。
  renderEntries() {
    if (!this.entries) return;
    const scroll = this.body?.scrollTop ?? 0;
    const files = this.plugin.highlightStore?.files || {};
    this.updateColorFilter(files);
    // 卡片底色规则是按 store 里的色值快照生成的，而那条规则通常由「正文绘制」顺带
    // 生成。旧记录（没有 colorHex）如果一直没被画过，规则里就没有它的色值，卡片会
    // 退回无色兜底。渲染前补一次；覆盖到位后 highlightPaletteHexes 命中，不再重建。
    const covered = this.plugin.highlightPaletteHexes;
    if (Object.values(files).some((anchors) =>
      anchors.some((anchor) => !covered?.has(anchorColorHex(anchor)))))
      this.plugin.applyHighlightPalette?.();
    const active = this.plugin.app.workspace.getActiveFile?.()?.path;
    const current = active || this.plugin.lastHighlightSourcePath;
    const needle = this.query.trim().toLocaleLowerCase();
    // 「当前文档 / 全部文档」管的是浏览范围，不该管搜索：输入关键词就放开到全部文档，
    // 否则在当前文档模式下按文档名搜别的文件永远搜不到，看起来像搜索不支持文档名。
    const paths = this.currentOnly && !needle
      ? (current ? [current] : [])
      : Object.keys(files).sort();
    // 计划：这一轮该按什么顺序摆哪些节点。build 只在这张卡确实要新建时才调用。
    const plan = [];
    for (const filePath of paths) {
      const anchors = (files[filePath] || []).filter((anchor) => {
        if (!anchor.id) return false;
        if (this.colorFilter !== "all" && highlightColorKey(anchor) !== this.colorFilter) return false;
        return !needle || [filePath, anchor.exact, anchor.note, ...(anchor.tags || [])]
          .some((part) => String(part || "").toLocaleLowerCase().includes(needle));
      });
      if (!anchors.length) continue;
      // 卡片自己已经带文档名了，「当前文档」模式下每条再重复同一个文件名没有意义；
      // 搜索会把别的文档一并列出来，那时分组标题就是必要的。
      // 分组标题自带条数，条数变了签名就变，所以它也会跟着重建。
      if (!this.currentOnly || needle) {
        plan.push({ key: `head:${filePath}`, signature: String(anchors.length), build: () => {
          const header = document.createElement("h4");
          header.textContent = `${filePath.split("/").pop()} · ${anchors.length}`;
          header.title = filePath;
          return header;
        } });
      }
      for (const anchor of anchors) {
        plan.push({
          key: `card:${anchor.id}`,
          signature: this.cardSignature(filePath, anchor),
          build: () => this.buildCard(filePath, anchor),
        });
      }
    }
    if (!plan.length) {
      plan.push({ key: "empty", signature: needle ? "search" : "none", build: () => {
        const empty = document.createElement("p");
        empty.className = "nh-sidebar-empty";
        empty.textContent = needle ? "没有匹配的高光" : "此范围还没有高光";
        return empty;
      } });
    }
    // 对位：签名没变的旧节点直接留下。唯独挂着就地编辑器的那张不能复用——收起
    // （保存/取消）靠的就是「重画这张卡」把编辑器换回预览框，复用会把这一步跳过；
    // 这样的卡片同一时刻只可能有一张（expandedId 锁着），从编辑器元素自己往上找即可，
    // 不必扫整棵子树。
    const editingCard = this.editorElement?.isConnected
      ? this.editorElement.closest(".nh-sidebar-card") : null;
    // 现有卡片按 key 收一份，遍历一次就够：后面清多余的节点也复用这张表，
    // 不再从头扫一遍 children（卡片多的时候这两遍遍历本身就是可观的开销）。
    const live = new Map();
    for (const child of this.entries.children) {
      const key = this.entryKeys.get(child);
      if (key) live.set(key, child);
    }
    const next = plan.map((item) => {
      const existing = live.get(item.key);
      if (existing && existing !== editingCard
          && this.entrySignatures.get(existing) === item.signature) return existing;
      const node = item.build();
      this.entryKeys.set(node, item.key);
      this.entrySignatures.set(node, item.signature);
      return node;
    });
    // 就位：只在节点确实不在它该在的位置时才搬。顺序变了也只挪真的错位的那些，
    // 绝不整块清空——清空的那一瞬滚动位置就没了。
    let placed = null;
    for (const node of next) {
      const at = placed ? placed.nextSibling : this.entries.firstChild;
      if (node !== at) this.entries.insertBefore(node, at);
      placed = node;
    }
    // 按节点本身比对，不是按 key：一张卡被重画时新旧节点共用一个 key，只比 key 会把
    // 那个已经不在 next 里的旧节点留下来。live 里收着全部有 key 的旧节点，扫它一遍即可，
    // 不必再去遍历一次 children。
    const wanted = new Set(next);
    for (const child of live.values()) if (!wanted.has(child)) child.remove();
    // 编辑器跟着它那张卡走：卡片被重画或被删，这个元素就脱离文档了。此时展开状态必须
    // 作废并回收临时图片 URL，否则 isEditing() 恒为真，60 ms 的后台刷新再也不进这个
    // 视图（见 scheduleHighlightSidebarRefresh）。
    if (this.editorElement && !this.editorElement.isConnected) {
      this.editorElement = null;
      this.expandedDispose?.();
      this.expandedDispose = null;
      this.expandedId = null;
    }
    if (scroll && this.body) this.body.scrollTop = scroll;
  }

  // 时间戳块：小字标出字段名，后面跟格式化后的时间。取不到时间就返回 null，调用方
  // 据此整块不渲染（旧记录没有 updatedAt，那一行本来就不该占位）。
  buildStamp(label, ms) {
    const text = highlightTimeLabel(ms);
    if (!text) return null;
    const stamp = document.createElement("span");
    stamp.className = "nh-sidebar-stamp";
    const name = document.createElement("span");
    name.className = "nh-sidebar-stamp-label";
    name.textContent = label;
    const value = document.createElement("span");
    value.textContent = text;
    // 卡片上是相对时间，悬停给出完整时间，PDF 页码之类需要精确时刻时才有地方可查。
    value.title = new Date(Number(ms)).toLocaleString();
    stamp.append(name, value);
    return stamp;
  }

  // 一条高光 = 一张卡片。顺序按用户指定：头部（色名 + 类型 + added_time）→ 划线原文
  // → changed_time → 文档名 + 页码 → 标签 → 按钮行 → 注释预览框。预览框在最后，点 ✎
  // 就是把它就地换成编辑器向下展开。
  //
  // 卡片本身不是按钮、不带整卡点击：HTML 不允许按钮嵌套（里面有按钮和编辑器），而且
  // 现在只有「跳转」按钮才跳转；顺带让划线原文变成可正常划选复制的普通文本。
  buildCard(filePath, anchor) {
    const card = document.createElement("div");
    // 底色 class 与 ::highlight 共用 renderedColor 后缀，具体色值由
    // applyHighlightPalette 按主题生成两套规则。
    const hex = anchorColorHex(anchor);
    card.className = `nh-sidebar-card nh-hl-${renderedColor(hex)}`;
    card.setAttribute("data-highlight-id", anchor.id);
    // 卡片底色是半透明的（见 applyHighlightPalette），划线原文左侧那道竖条要的是实色，
    // 顺手把原色挂在卡片上给 CSS 用。
    card.style.setProperty("--nh-card-accent", hex);
    // 就地编辑时同行那对「保存/取消」按卡片色上色，实色底上的字色得跟着底色走才有对比度
    // （和色块 swatch 用的是同一个 readableOnColor）。
    card.style.setProperty("--nh-card-accent-text", readableOnColor(hex));

    const head = document.createElement("div");
    head.className = "nh-sidebar-head";
    const swatch = document.createElement("span");
    swatch.className = "nh-sidebar-color";
    // 颜色是记录当时的快照：名字必须跟着这条记录走，不能跟别处的设置走。
    swatch.textContent = anchorColorLabel(anchor);
    swatch.style.background = hex;
    swatch.style.color = readableOnColor(hex);
    head.appendChild(swatch);
    for (const label of [anchor.kind === "pdf" ? "pdf" : "markdown", "highlight"]) {
      const chip = document.createElement("span");
      chip.className = "nh-sidebar-chip";
      chip.textContent = label;
      head.appendChild(chip);
    }
    const added = this.buildStamp("added_time", anchor.createdAt);
    if (added) head.appendChild(added);
    card.appendChild(head);

    const quote = document.createElement("p");
    quote.className = "nh-sidebar-quote";
    quote.textContent = anchor.exact || "（原文已失效）";
    card.appendChild(quote);

    // 注释标题紧跟在划线原文下面，和原文一样是「引文式」的一行小字；没设标题就不占位。
    const title = this.plugin.buildAnnotationTitle(anchor);
    if (title) card.appendChild(title);

    // 只在真的改过注释、且与记录时间不同时才显示，否则同一时刻重复两遍。
    if (anchor.updatedAt && anchor.updatedAt !== anchor.createdAt) {
      const changed = this.buildStamp("changed_time", anchor.updatedAt);
      if (changed) card.appendChild(changed);
    }

    const meta = document.createElement("div");
    meta.className = "nh-sidebar-meta";
    const file = document.createElement("span");
    file.className = "nh-sidebar-file";
    file.textContent = filePath.split("/").pop();
    file.title = filePath;
    meta.appendChild(file);
    if (anchor.kind === "pdf" && anchor.page) {
      const page = document.createElement("span");
      page.className = "nh-sidebar-page";
      page.textContent = `p.${anchor.page}`;
      meta.appendChild(page);
    }
    card.appendChild(meta);

    if (anchor.tags?.length) {
      const tags = document.createElement("div");
      tags.className = "nh-sidebar-tags";
      tags.textContent = anchor.tags.join("  ");
      card.appendChild(tags);
    }

    const actions = document.createElement("div");
    actions.className = "nh-sidebar-actions";
    actions.append(
      this.buildAction("square-pen", "编辑注释", () => this.toggleEditor(card, anchor)),
      this.buildAction("external-link", "跳转到原文", () =>
        this.plugin.openHighlightById(anchor.id).catch((error) =>
          new Notice(`高光定位失败：${error.message || error}`))),
      // 只导出这一条到桌面端配置好的索引文档与注释目录，目的是把它并进「记录问题」
      // 那套笔记里；批量导出走高光页的按钮，两者互不影响。
      this.buildAction("file-output", "导出这一条", () =>
        this.plugin.exportSingleHighlight(anchor.id)
          .then((result) => new Notice(`已导出到 ${result.index}`))
          .catch((error) => new Notice(`导出失败：${error.message || error}`))),
      this.buildAction("trash-2", "删除这条高光及注释", () => this.removeEntry(anchor.id)),
    );
    card.appendChild(actions);

    // 没有注释就不占位，但 ✎ 仍在，点开即可补一条。预览框限高（styles.css 的
    // .nh-sidebar-body，上限仍是 min(8em, 26vh)，不随内容长高），超出的部分框内滚动，
    // 和鼠标悬停高光弹出的预览一致：滚轮落在框上先滚框，框内没超上限才交给侧栏。
    if (anchor.note || anchor.noteLayout || anchor.images?.length) {
      const preview = document.createElement("div");
      preview.className = "nh-sidebar-body";
      this.plugin.renderAnnotationBody(preview, anchor, { scrollable: true });
      card.appendChild(preview);
    }
    return card;
  }

  buildAction(icon, label, handler) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "clickable-icon nh-sidebar-action";
    button.title = label;
    button.setAttribute("aria-label", label);
    setIcon(button, icon);
    button.addEventListener("click", () => { handler(); });
    return button;
  }

  // 编辑注释：用编辑器替换掉卡片里的注释预览框，就地向卡片下方展开，不弹窗。
  // 编辑器本身与正文里点高亮弹出的那个共用 buildAnnotationEditor，图片落盘、标签
  // 校验与失败回滚因此只有一份实现。
  toggleEditor(card, anchor) {
    // 再点一次 ✎ 就是收起。这里必须整块重渲染：只 dispose 的话，卡片里留着的是那个
    // 图片临时 URL 已被回收的编辑器——既没收回预览态，又比预览框大一截，像被撑住了。
    if (this.expandedId === anchor.id) {
      this.collapseEditor();
      this.renderEntries();
      return;
    }
    // 换一条编辑时同样先把上一条收回预览态，否则它也会停在一个已 dispose 的编辑器上。
    const switching = Boolean(this.expandedId);
    this.collapseEditor();
    if (switching) {
      this.renderEntries();
      card = [...(this.entries?.children || [])]
        .find((element) => element.getAttribute("data-highlight-id") === anchor.id) || null;
      if (!card) return;
    }
    const done = () => {
      // 收起要走 collapseEditor 而不是把两个字段置空：dispose 才会回收编辑器里图片的
      // 临时 URL，也才会把挂在卡片按钮行上的「保存/取消」摘掉。以前这里靠随后整块重建
      // 把 DOM 丢掉，URL 就漏在那了；现在卡片按签名复用，更不能指望重建来兜底。
      this.collapseEditor();
      this.renderEntries();
    };
    const built = this.plugin.buildAnnotationEditor({
      id: anchor.id, anchor, doc: document, onSaved: done, onCancel: done,
      // 「保存/取消」挂到卡片上 ✎/↗/🗑 那一行的右端：按钮和操作图标同一行，就在编辑框
      // 上方一步之内，不必滚到底部去找（编辑器自己的按钮行因此在侧栏不存在）。
      actionsEl: card.querySelector(".nh-sidebar-actions"),
    });
    // 复用预览框的 class，展开前后占的是同一块位置、同样的限高与滚动行为。
    built.element.classList.add("nh-sidebar-body", "nh-sidebar-editor");
    const preview = card.querySelector(".nh-sidebar-body");
    if (preview) preview.replaceWith(built.element);
    else card.appendChild(built.element);
    this.expandedId = anchor.id;
    this.expandedDispose = built.dispose;
    // 记下这个元素本身：renderEntries 用它认出「哪张卡挂着编辑器、不能复用」。
    this.editorElement = built.element;
    built.element.querySelector("textarea")?.focus();
  }

  collapseEditor() {
    if (this.expandedDispose) this.expandedDispose();
    this.expandedDispose = null;
    this.expandedId = null;
  }

  removeEntry(id) {
    if (!window.confirm("删除这条高光及注释？源文件不会改变。")) return;
    this.plugin.changeHighlight(id, {}, true)
      .then(() => this.renderEntries())
      .catch((error) => new Notice(`删除高光失败：${error.message || error}`));
  }
}

class NotingHelperBridge extends Plugin {
  async onload() {
    const saved = (await this.loadData()) || {};
    const vaultIdentity = this.app.vault.adapter.getBasePath
      ? this.app.vault.adapter.getBasePath()
      : this.app.vault.getName();
    const portOffset = parseInt(
      crypto.createHash("sha256").update(vaultIdentity).digest("hex").slice(0, 6),
      16
    ) % 1000;
    this.settings = {
      port: Number(saved.port) || DEFAULT_PORT + portOffset,
      token: saved.token || crypto.randomBytes(24).toString("hex"),
      // 旧地址仅用于首次无损迁移；新运行时只向插件内部的固定日志写入。
      highlightJournal: saved.highlightJournal || "NoteDataOfY/高光库/高光数据.jsonl",
      // 颜色自 2.4.0 起固定为 8 种（HIGHLIGHT_COLOR_DEFS），不再有可持久化的调色板；
      // 旧 data.json 里的 highlightColors 在这套「只挑认识的字键」里自然被忽略。
      highlightSingleExport: (() => {
        const raw = saved.highlightSingleExport;
        if (!raw || typeof raw !== "object") return { indexPath: "", noteDir: "" };
        return {
          indexPath: String(raw.indexPath || ""),
          noteDir: String(raw.noteDir || ""),
        };
      })(),
      // 悬浮窗上摆哪 3 种颜色（8 选 3）。旧 data.json 没这个键就走默认。
      nativeHighlightColors: normalizeActiveColors(saved.nativeHighlightColors),
      nativeHighlightEntry: saved.nativeHighlightEntry === true,
    };
    this.fixedHighlightJournal = `${this.manifest.dir}/storage/highlights.jsonl`;
    this.captures = new Map();
    this.highlightInFlight = new Map();
    this.desktopModeWaiter = null;
    this.pendingAnchorEdits = new Map();
    this.anchorEditFlushTimer = null;
    this.lastHighlightMs = 0;
    this.paneStates = new Map();
    this.paneButtons = new Map();
    this.lastHandledLink = null;
    this.lastHandledAt = 0;
    this.debugState = { event: "none" };
    this.editorInterceptors = new Map();
    this.pointerDownHandler = (event) => this.handleDocumentMouseDown(event);
    this.mouseDownHandler = (event) => this.handleDocumentMouseDown(event);
    this.clickHandler = (event) => this.handleDocumentClick(event);
    window.addEventListener("pointerdown", this.pointerDownHandler, true);
    window.addEventListener("click", this.clickHandler, true);
    document.addEventListener("mousedown", this.mouseDownHandler, true);
    this.register(() => {
      window.removeEventListener("pointerdown", this.pointerDownHandler, true);
      window.removeEventListener("click", this.clickHandler, true);
      document.removeEventListener("mousedown", this.mouseDownHandler, true);
      this.clearPaneButtons();
      this.clearEditorInterceptors();
      if (this.pdfObserver) this.pdfObserver.disconnect();
      if (this.pdfRefreshTimer) clearTimeout(this.pdfRefreshTimer);
      if (this.highlightRetryTimer) clearTimeout(this.highlightRetryTimer);
      if (this.anchorEditFlushTimer) clearTimeout(this.anchorEditFlushTimer);
      if (this.pendingAnchorEdits.size) this.flushAnchorEdits().catch(() => {});
    });
    // 高光锚点住在这里，**不住在正文里**（见 loadHighlights 的说明）
    this.highlightStore = null;
    // 仅缓存当前打开文件的注释视图，持久化源仍是 JSONL 重放后的 highlightStore。
    this.openAnnotationCache = new Map();
    this.pdfObserver = null;
    this.pdfRefreshTimer = null;
    this.highlightRetryTimer = null;
    this.highlightRetryCount = 0;
    this.readingSurfaces = new Set();
    this.interactiveRanges = [];
    await this.loadHighlights();
    await this.saveData(this.settings);
    this.applyHighlightPalette();
    this.register(() => { this.highlightPaletteStyle?.remove(); this.highlightPaletteStyle = null; });
    this.registerEditorExtension(editorMarks);
    this.registerEditorExtension(EditorView.updateListener.of((update) => this.handleEditorUpdate(update)));
    this.registerEvent(this.app.workspace.on("layout-change", () => {
      this.refreshPaneButtons();
      this.refreshEditorInterceptors();
      this.syncOpenAnnotationCache();
      this.watchHighlightSurfaces();
      this.scheduleEditorDecorationRefresh();
      this.flushAnchorEdits().catch((error) => { this.lastAnchorEditError = String(error); });
    }));
    // PDF 的容器是随分区/分页新建的，换文件或换活动分区都要重挂观察
    this.registerEvent(this.app.workspace.on("file-open", (file) => {
      if (file?.path) this.lastHighlightSourcePath = file.path;
      this.scheduleHighlightSidebarRefresh?.();
      this.syncOpenAnnotationCache();
      this.watchHighlightSurfaces();
      this.scheduleEditorDecorationRefresh();
    }));
    this.registerEvent(this.app.workspace.on("active-leaf-change", () => {
      this.syncOpenAnnotationCache();
      this.watchHighlightSurfaces();
    }));
    this.registerReadingViewHighlights();
    this.registerView(HIGHLIGHT_SIDEBAR_TYPE, (leaf) => new HighlightSidebarView(leaf, this));
    this.addRibbonIcon("panel-right-open", "打开高光索引与注释侧栏", () =>
      void this.openHighlightSidebar().catch((error) => new Notice(`打开高光侧栏失败：${error.message || error}`)));
    this.addCommand({ id: "open-highlight-sidebar", name: "打开高光索引侧栏",
      callback: () => void this.openHighlightSidebar() });
    this.register(() => { if (this.sidebarRefreshTimer) clearTimeout(this.sidebarRefreshTimer); });
    this.registerDomEvent(document, "click", (event) => this.onHighlightClick(event));
    this.registerDomEvent(document, "mousemove", (event) => this.onHighlightMove(event));
    // In-Obsidian selection entry: no Tk capture, clipboard copy, pipe or HTTP on the click path.
    this.registerDomEvent(document, "mouseup", (event) => this.onNativeSelectionEnd(event));
    this.registerDomEvent(document, "keyup", (event) => {
      if (event.key === "Shift" || (event.shiftKey && event.key.startsWith("Arrow")))
        this.onNativeSelectionEnd(event);
    });
    this.registerDomEvent(document, "mousedown", (event) => {
      if (this.nativePalette && !this.nativePalette.contains(event.target)) this.hideNativePalette();
    }, true);
    this.register(() => this.hideNativePalette());
    this.register(() => { this.hideHighlightPreview(); this.closeHighlightMenu(); });
    this.registerObsidianProtocolHandler("noting-helper-highlight", (params) => {
      if (params.vault && params.vault !== this.app.vault.getName()) return;
      this.openHighlightById(String(params.id || "")).catch((error) =>
        new Notice(`高光跳转失败：${error.message || error}`));
    });
    this.app.workspace.onLayoutReady(() => {
      this.refreshPaneButtons();
      this.refreshEditorInterceptors();
      this.syncOpenAnnotationCache();
      this.watchHighlightSurfaces();
      this.scheduleEditorDecorationRefresh();
    });
    this.addCommand({
      id: "show-pane-jump-diagnostics",
      name: "显示原文跳转诊断",
      callback: () => {
        const armed = this.armedPane();
        const state = this.debugState || {};
        new Notice(
          `目标分区：${armed ? "已设置" : "未设置"}；最近事件：${state.event || "none"}` +
          `${state.reason ? `；原因：${state.reason}` : ""}`,
          7000
        );
      },
    });
    this.addCommand({
      id: "toggle-native-highlight-entry",
      name: "切换 Obsidian 内的划选高光入口",
      callback: () => this.configureNativeHighlightEntry(!this.settings.nativeHighlightEntry)
        .then((enabled) => {
          if (enabled) this.requestDesktopMode("highlight");
          new Notice(`Obsidian 内高光入口已${enabled ? "启用" : "关闭"}`);
        })
        .catch((error) => new Notice(`切换高光入口失败：${error.message || error}`)),
    });
    this.addCommand({
      id: "show-highlight-diagnostics",
      name: "显示高光诊断",
      callback: () => {
        this.refreshHighlights();
        new Notice(this.highlightDiagnostics(), 15000);
      },
    });
    this.startServer();
  }

  onunload() {
    if (this.server) {
      this.server.close();
      this.server = null;
    }
    for (const socket of this.pipeSockets || []) socket.destroy();
    if (this.pipeServer) this.pipeServer.close();
    this.pipeSockets?.clear();
    this.pipeServer = null;
    this.captures.clear();
    this.finishDesktopModeWait(null);
    if (this.editorRefreshTimer) clearTimeout(this.editorRefreshTimer);
  }

  async openHighlightSidebar() {
    let leaf = this.app.workspace.getLeavesOfType(HIGHLIGHT_SIDEBAR_TYPE)[0];
    if (!leaf) {
      leaf = this.app.workspace.getRightLeaf(false);
      if (!leaf) throw new Error("无法打开右侧栏。");
      await leaf.setViewState({ type: HIGHLIGHT_SIDEBAR_TYPE, active: true });
    }
    await this.app.workspace.revealLeaf(leaf);
    leaf.view?.render?.();
  }

  // 从正文的高光菜单反向定位到侧栏那张卡：先确保侧栏打开，再等视图真的能定位。
  // 不直接 await openHighlightSidebar() 就用 view，是因为 onOpen 和 setViewState 的
  // 完成次序没有保证——视图对象可能在 setViewState 兑现时还是空壳，或者被视图重载换掉。
  async revealHighlightInSidebar(id) {
    await this.openHighlightSidebar();
    for (let attempt = 0; attempt < 20; attempt += 1) {
      const leaf = this.app.workspace.getLeavesOfType(HIGHLIGHT_SIDEBAR_TYPE)[0];
      if (leaf?.view?.revealHighlight?.(id)) return true;
      await new Promise((resolve) => setTimeout(resolve, 50));
    }
    throw new Error("高光侧栏未能及时打开。");
  }

  scheduleHighlightSidebarRefresh() {
    if (!this.app?.workspace?.getLeavesOfType?.(HIGHLIGHT_SIDEBAR_TYPE)?.length
        || this.sidebarRefreshTimer) return;
    this.sidebarRefreshTimer = setTimeout(() => {
      this.sidebarRefreshTimer = null;
      for (const leaf of this.app.workspace.getLeavesOfType(HIGHLIGHT_SIDEBAR_TYPE)) {
        // 正在就地编辑的那条会重建卡片、连带丢掉未保存的输入，等它自己收起来再刷。
        if (leaf.view?.isEditing?.()) continue;
        leaf.view?.renderEntries?.();
      }
    }, 60);
  }

  clearPaneButtons() {
    for (const buttons of this.paneButtons.values()) {
      for (const button of buttons) button.remove();
    }
    this.paneButtons.clear();
    this.paneStates.clear();
  }

  clearEditorInterceptors() {
    for (const [contentDOM, handler] of this.editorInterceptors.entries()) {
      for (const eventName of ["pointerdown", "mousedown", "click"]) {
        contentDOM.removeEventListener(eventName, handler, true);
      }
    }
    this.editorInterceptors.clear();
  }

  refreshEditorInterceptors() {
    if (!this.app || !this.app.workspace) return;
    const seen = new Set();
    this.app.workspace.iterateAllLeaves((leaf) => {
      const view = leaf && leaf.view;
      const cm = view && view.editor && view.editor.cm;
      const contentDOM = cm
        ? cm.contentDOM || (cm.dom && cm.dom.contentDOM) ||
          (typeof cm.getWrapperElement === "function" ? cm.getWrapperElement() : null)
        : null;
      if (!contentDOM || typeof contentDOM.addEventListener !== "function") return;
      seen.add(contentDOM);
      if (this.editorInterceptors.has(contentDOM)) return;
      const handler = (event) => {
        if (!isPlainLeftClick(event) || event[HANDLED_EVENT]) return;
        if (this.navigateOriginalLink(event, leaf)) {
          event[HANDLED_EVENT] = true;
          event.stopImmediatePropagation();
        }
      };
      for (const eventName of ["pointerdown", "mousedown", "click"]) {
        contentDOM.addEventListener(eventName, handler, true);
      }
      this.editorInterceptors.set(contentDOM, handler);
    });
    for (const [contentDOM, handler] of this.editorInterceptors.entries()) {
      if (seen.has(contentDOM) && contentDOM.isConnected) continue;
      for (const eventName of ["pointerdown", "mousedown", "click"]) {
        contentDOM.removeEventListener(eventName, handler, true);
      }
      this.editorInterceptors.delete(contentDOM);
    }
  }

  paneForElement(element) {
    return element && typeof element.closest === "function"
      ? element.closest(".workspace-leaf")
      : null;
  }

  leafForElement(element, pane) {
    let result = null;
    this.app.workspace.iterateAllLeaves((leaf) => {
      if (result || !leaf || !leaf.view) return;
      const container = leaf.view.containerEl;
      if (element && container && typeof container.contains === "function" && container.contains(element)) {
        result = leaf;
      }
    });
    if (result) return result;
    this.app.workspace.iterateAllLeaves((leaf) => {
      if (result || !leaf || !leaf.view) return;
      if (this.paneForElement(leaf.view.containerEl) === pane) result = leaf;
    });
    return result;
  }

  refreshPaneButtons() {
    if (!this.app || !this.app.workspace) return;
    const seenPanes = new Set();
    this.app.workspace.iterateAllLeaves((leaf) => {
      const container = leaf && leaf.view && leaf.view.containerEl;
      if (!container || typeof container.querySelector !== "function") return;
      const pane = this.paneForElement(container);
      const actions = container.querySelector(".view-actions");
      if (!pane || !actions) return;
      seenPanes.add(pane);
      if (!this.paneStates.has(pane)) this.paneStates.set(pane, false);
      let buttons = this.paneButtons.get(pane);
      if (!buttons) {
        buttons = new Set();
        this.paneButtons.set(pane, buttons);
      }
      let button = actions.querySelector(".notinghelper-pane-toggle");
      if (!button) {
        button = actions.createEl("button", {
          cls: ["clickable-icon", "notinghelper-pane-toggle"],
          attr: {
            type: "button",
            "aria-label": "原文跳转分区：关闭",
            title: "原文跳转分区：关闭",
          },
        });
        this.registerDomEvent(button, "click", (event) => {
          event.preventDefault();
          event.stopPropagation();
          this.togglePane(pane);
        });
      }
      buttons.add(button);
      this.updatePaneButtons(pane);
    });
    for (const [pane, buttons] of this.paneButtons.entries()) {
      if (!seenPanes.has(pane) || !pane.isConnected) {
        for (const button of buttons) button.remove();
        this.paneButtons.delete(pane);
        this.paneStates.delete(pane);
      }
    }
  }

  updatePaneButtons(pane) {
    const open = this.paneStates.get(pane) === true;
    const buttons = this.paneButtons.get(pane) || [];
    for (const button of buttons) {
      setIcon(button, open ? "book-open" : "book");
      button.classList.toggle("is-open", open);
      button.setAttribute("aria-label", `原文跳转分区：${open ? "打开" : "关闭"}`);
      button.setAttribute("title", `原文跳转分区：${open ? "打开" : "关闭"}`);
    }
  }

  togglePane(pane) {
    if (!pane) return;
    const next = this.paneStates.get(pane) !== true;
    for (const otherPane of this.paneStates.keys()) {
      this.paneStates.set(otherPane, otherPane === pane && next);
      this.updatePaneButtons(otherPane);
    }
  }

  armedPane() {
    for (const [pane, open] of this.paneStates.entries()) {
      if (open === true && pane.isConnected) return pane;
    }
    return null;
  }

  sourceModeLinkFromEvent(event, preferredLeaf = null) {
    if (!event || !this.app || !this.app.workspace) return null;
    let result = null;
    const leaves = [];
    if (preferredLeaf) leaves.push(preferredLeaf);
    this.app.workspace.iterateAllLeaves((leaf) => {
      if (!leaves.includes(leaf)) leaves.push(leaf);
    });
    const path = eventElementPath(event);
    const coordinates = Number.isFinite(event.clientX) && Number.isFinite(event.clientY)
      ? { x: event.clientX, y: event.clientY }
      : null;
    for (const leaf of leaves) {
      if (result || !leaf || !leaf.view || !leaf.view.editor) continue;
      const container = leaf.view.containerEl;
      const containsTarget = container && typeof container.contains === "function" &&
        path.some((item) => item && item.nodeType && container.contains(item));
      if (!containsTarget) continue;
      const cm = leaf.view.editor.cm;
      if (!cm || typeof cm.posAtCoords !== "function" || !cm.state || !cm.state.doc) continue;
      let position;
      if (coordinates) {
        try {
          position = cm.posAtCoords(coordinates);
        } catch (_error) {
          position = null;
        }
      }
      if (position && typeof position === "object") position = position.pos;
      if (!Number.isInteger(position)) continue;
      let line;
      try {
        line = cm.state.doc.lineAt(position);
      } catch (_error) {
        continue;
      }
      const offset = position - line.from;
      const info = parseWikiLinkAtOffset(line.text, offset) ||
        parseWikiLinkAtOffset(line.text, offset - 1) ||
        parseWikiLinkAtOffset(line.text, offset + 1);
      if (info) result = { info, element: container, leaf, line: line.text, offset };
    }
    return result;
  }

  handleDocumentMouseDown(event) {
    if (!isPlainLeftClick(event)) return;
    const link = linkElementFromEvent(event);
    if (link && link === this.lastHandledLink && Date.now() - this.lastHandledAt < 1000) {
      event.preventDefault();
      event.stopImmediatePropagation();
      return;
    }
    if (this.navigateOriginalLink(event)) {
      event[HANDLED_EVENT] = true;
      this.lastHandledLink = link;
      this.lastHandledAt = Date.now();
      event.stopImmediatePropagation();
    }
  }

  handleDocumentClick(event) {
    if (!isPlainLeftClick(event)) return;
    if (event[HANDLED_EVENT]) return;
    const link = linkElementFromEvent(event);
    if (link && link === this.lastHandledLink && Date.now() - this.lastHandledAt < 1000) {
      event.preventDefault();
      event.stopImmediatePropagation();
      return;
    }
    if (this.navigateOriginalLink(event)) event[HANDLED_EVENT] = true;
  }

  navigateOriginalLink(event, preferredLeaf = null) {
    let link = linkElementFromEvent(event);
    let info = originalLinkInfo(link);
    let sourceMode = null;
    if (!info) {
      sourceMode = this.sourceModeLinkFromEvent(event, preferredLeaf);
      link = sourceMode && sourceMode.element;
      info = sourceMode && sourceMode.info;
    }
    this.debugState = {
      event: "received",
      element: link && link.className ? String(link.className) : "none",
      reason: info ? `parsed:${info.href}` : "link-not-recognized",
      sourceLeaf: sourceMode && sourceMode.leaf && sourceMode.leaf.view && sourceMode.leaf.view.file
        ? sourceMode.leaf.view.file.path
        : null,
      line: sourceMode ? sourceMode.line : null,
      offset: sourceMode ? sourceMode.offset : null,
    };
    if (!info) return false;
    const sourcePane = this.paneForElement(link);
    const targetPane = this.armedPane();
    if (!sourcePane || !targetPane) {
      this.debugState.reason = `${!sourcePane ? "source-pane-missing" : "target-pane-missing"}`;
      return false;
    }
    const sourceLeaf = sourceMode ? sourceMode.leaf : this.leafForElement(link, sourcePane);
    const targetLeaf = this.leafForElement(null, targetPane);
    const sourcePath = sourceLeaf && sourceLeaf.view && sourceLeaf.view.file
      ? sourceLeaf.view.file.path
      : this.app.workspace.getActiveFile()
        ? this.app.workspace.getActiveFile().path
        : "";
    event.preventDefault();
    event.stopImmediatePropagation();
    if (targetLeaf && typeof targetLeaf.openFile === "function") {
      const hashIndex = info.href.indexOf("#");
      const linkPath = hashIndex >= 0 ? info.href.slice(0, hashIndex) : info.href;
      const subpath = hashIndex >= 0 ? info.href.slice(hashIndex) : "";
      const file = this.app.metadataCache.getFirstLinkpathDest(linkPath, sourcePath);
      if (file) {
        this.debugState.reason = `opening:${file.path}`;
        this.app.workspace.setActiveLeaf(targetLeaf, { focus: true });
        targetLeaf.openFile(file, subpath ? { eState: { subpath } } : undefined);
        return true;
      }
    }
    if (targetLeaf) this.app.workspace.setActiveLeaf(targetLeaf, { focus: true });
    this.debugState.reason = "fallback-openLinkText";
    this.app.workspace.openLinkText(info.href, sourcePath, false);
    return true;
  }

  startServer() {
    this.server = http.createServer((request, response) => {
      this.handleRequest(request, response).catch((error) => {
        sendJson(response, 500, { error: error.message || "bridge error" });
      });
    });
    this.server.on("error", (error) => {
      new Notice(`Noting Helper Bridge 启动失败：${error.message}`);
    });
    this.server.listen(this.settings.port, "127.0.0.1");
    // 命名管道为本机请求提供短连接；沿用 handleRequest 的鉴权、幂等、错误响应。
    // 不做请求后的 HTTP 自动重发：断线时高光可能已经写盘。
    if (process.platform === "win32") this.startPipeServer();
  }

  startPipeServer() {
    const address = highlightPipeName(this.settings.token, this.settings.port);
    this.pipeSockets = new Set();
    this.pipeServer = net.createServer((socket) => {
      this.pipeSockets.add(socket);
      socket.on("close", () => this.pipeSockets.delete(socket));
      let pending = Buffer.alloc(0);
      let pendingFirstCallbackWallMs = null;
      let pendingLastCallbackWallMs = null;
      let processing = false;
      let partialTimer = null;
      const clearPartialTimer = () => { if (partialTimer) clearTimeout(partialTimer); partialTimer = null; };
      const armPartialTimer = () => {
        clearPartialTimer();
        // The timeout protects only a *partial frame*. A complete queued frame
        // may legitimately wait for a slow preceding request on this socket.
        if (pending.length && (pending.length < 4
            || pending.length < pending.readUInt32BE(0) + 4)) {
          partialTimer = setTimeout(() => socket.destroy(), 6000);
        }
      };
      const processFrames = async () => {
        if (processing) return;
        processing = true;
        try {
          while (!socket.destroyed && pending.length >= 4) {
            const expected = pending.readUInt32BE(0);
            if (!expected || expected > 64 * 1024) { socket.destroy(); return; }
            if (pending.length < expected + 4) break;
            const frame = pending.subarray(4, expected + 4);
            const pipeTrace = { pipe_first_data_callback_wall_ms: pendingFirstCallbackWallMs,
              pipe_data_callback_wall_ms: pendingLastCallbackWallMs,
              pipe_frame_dequeued_wall_ms: Date.now() };
            pending = pending.subarray(expected + 4);
            if (pending.length) armPartialTimer();
            else { clearPartialTimer(); pendingFirstCallbackWallMs = pendingLastCallbackWallMs = null; }
            let packet;
            try { packet = JSON.parse(frame.toString("utf8")); }
            catch (_error) { socket.destroy(); return; }
            if (typeof packet !== "object" || !packet || Array.isArray(packet)
                || typeof packet.route !== "string" || !packet.route.startsWith("/")
                || typeof packet.token !== "string") { socket.destroy(); return; }
            // 多帧复用同一条连接，但每帧仍按原鉴权/业务路由串行处理。
            const { Readable } = require("stream");
            const request = Readable.from([JSON.stringify(packet.payload || {})]);
            request.method = packet.method === "GET" ? "GET" : "POST";
            request.url = packet.route;
            request.headers = { authorization: `Bearer ${packet.token}` };
            if (packet.payload?.trace === true) request._nhPipeTrace = pipeTrace;
            const response = {
              destroyed: false, writableEnded: false, statusCode: 200,
              writeHead(status) { this.statusCode = status; },
              end(body) {
                if (this.writableEnded || socket.destroyed) return;
                this.writableEnded = true;
                const result = Buffer.from(JSON.stringify({ status: this.statusCode,
                  body: JSON.parse(body || "{}") }), "utf8");
                const header = Buffer.allocUnsafe(4);
                header.writeUInt32BE(result.length, 0);
                socket.write(Buffer.concat([header, result]));
              },
            };
            try { await this.handleRequest(request, response); }
            catch (error) { sendJson(response, 500, { error: error.message || "bridge error" }); }
          }
        } finally {
          processing = false;
          if (pending.length) armPartialTimer(); else clearPartialTimer();
          if (pending.length >= 4 && !socket.destroyed
              && pending.length >= 4 + pending.readUInt32BE(0)) void processFrames();
        }
      };
      socket.on("data", (chunk) => {
        // Earliest JS-observable point; not the kernel's actual arrival timestamp.
        const callbackWallMs = Date.now();
        if (!pending.length) pendingFirstCallbackWallMs = callbackWallMs;
        pendingLastCallbackWallMs = callbackWallMs;
        pending = Buffer.concat([pending, chunk]);
        if (pending.length > 1024 * 1024) { socket.destroy(); return; }
        armPartialTimer();
        void processFrames();
      });
      socket.on("close", clearPartialTimer);
      socket.on("error", () => {});
    });
    this.pipeServer.on("error", (error) => {
      this.lastPipeError = String(error.message || error);
      this.pipeServer = null;
    });
    this.pipeServer.listen(address);
  }

  isAuthorized(request) {
    return request.headers.authorization === `Bearer ${this.settings.token}`;
  }

  purgeExpiredCaptures() {
    const cutoff = Date.now() - CAPTURE_TTL_MS;
    for (const [captureId, capture] of this.captures.entries()) {
      if (capture.createdAt < cutoff) this.captures.delete(captureId);
    }
  }

  // --------------------------------------------------------------- 锚点存储
  //
  // 锚点在插件目录下：旧 highlights.json 只读；新记录按文件追加日志。
  // 不塞进 data.json（onload 的 saveData 只保存 {port, token}），安装也不会覆盖数据。
  highlightStorePath() {
    return `${this.manifest.dir}/highlights.json`;
  }

  // 一条命令说清「高光为什么没显示」。高光这条链断在哪一段，从外面看不出来
  // （画不出来不报错），所以把每一段都报出来：Highlight API 有没有、锚点读到了
  // 几条、视图容器找到几个、实际算出多少段颜色。
  highlightDiagnostics() {
    const registry = highlightRegistry();
    const lines = [];
    lines.push(`Highlight API：${registry ? "可用" : "不可用 → 高光不会显示"}`);
    const files = (this.highlightStore && this.highlightStore.files) || {};
    const names = Object.keys(files);
    const total = names.reduce((sum, name) => sum + files[name].length, 0);
    lines.push(`锚点：${total} 条，分布在 ${names.length} 个文件`);
    for (const name of names.slice(0, 5)) {
      lines.push(`  ${name} → ${files[name].length} 条`);
    }
    // 颜色形态不对是「画不出来还不报错」的头号原因，单独点出来
    let odd = 0;
    for (const name of names) {
      for (const anchor of files[name]) {
        // 颜色固定成 8 种之后，既认不出 color 也认不出 colorHex 的记录才真的画不出来。
        if (!normalizeHighlightColor(anchor.color) && !normalizeHighlightColor(anchor.colorHex)) odd += 1;
      }
    }
    if (odd) lines.push(`其中 ${odd} 条的颜色格式不受支持`);
    lines.push(`PDF 视图：${this.pdfContainers().length} 个`);
    // DOM 层的实况要一并报出来：选择器对不上时，「有视图但找不到文本层」和
    // 「文本层在但文字对不上」是两种完全不同的毛病，光看「没颜色」分不出来。
    for (const { container, filePath } of this.pdfContainers().slice(0, 3)) {
      const pages = pdfPageNumbers(container).length;
      const layers = container.querySelectorAll(".textLayer").length;
      const spans = container.querySelectorAll(".textLayer span").length;
      lines.push(`  ${filePath}：${pages} 页 / ${layers} 个 .textLayer / ${spans} 个 span`);
      // 逐条报出这条锚点卡在哪一步——「没颜色」这三个字本身分不出是没滚到那页、
      // 页没渲染、还是文字对不上。
      for (const anchor of this.anchorsForFile(filePath).filter(
        (item) => item.kind === "pdf",
      ).slice(0, 3)) {
        const text = String(anchor.exact || "").slice(0, 12);
        lines.push(`    p.${anchor.page}「${text}」→ ${this.pdfAnchorStatus(container, anchor)}`);
      }
    }
    const editors = this.markdownContentRoots();
    lines.push(`Markdown 编辑视图：${editors.length} 个（CodeMirror 装饰）`);
    for (const { cm, filePath } of editors.slice(0, 2)) {
      const field = cm.state.field(editorMarks, false);
      lines.push(`  ${filePath}：${field ? field.size : 0} 段装饰`);
    }
    if (this.highlightStoreError) lines.push(this.highlightStoreError);
    lines.push(`最近一次日志追加：${this.lastHighlightWriteMs || 0} ms（目录 ${this.lastHighlightMkdirMs || 0} / 追加 ${this.lastHighlightAppendMs || 0} ms）`);
    if (registry) {
      for (const color of HIGHLIGHT_COLORS) {
        const painted = registry.get(highlightRegistryName(color));
        lines.push(`  ${color} 已画 ${painted ? painted.size : 0} 段`);
      }
    }
    lines.push(`本次收集：切出 ${this.lastRangeCount || 0} 段可上色的区间，最近重画 ${this.lastHighlightRefreshMs || 0} ms`);
    for (const note of (this.collectNotes || []).slice(0, 8)) {
      lines.push(`  · ${note}`);
    }
    if (this.lastRangeError) lines.push(`  上色时出错：${this.lastRangeError}`);
    if (this.lastEditorError) lines.push(`  ${this.lastEditorError}`);
    if (this.lastSelectionError) lines.push(`  收起选区：${this.lastSelectionError}`);
    if (this.lastAnchorEditError) lines.push(`  ${this.lastAnchorEditError}`);
    return lines.join("\n");
  }

  highlightJournalDir() {
    return `${this.manifest.dir}/highlight-journal`;
  }

  highlightJournalName(filePath) {
    return `${crypto.createHash("sha256").update(filePath).digest("hex")}.jsonl`;
  }

  async loadHighlights() {
    if (this.highlightStore) return this.highlightStore;
    const adapter = this.app.vault.adapter;
    const target = this.fixedHighlightJournal || `${this.manifest.dir}/storage/highlights.jsonl`;
    let store = { version: 2, files: {} };
    // 一旦私有日志存在，绝不再读旧库：旧库可能包含已经 reset 的测试高光。
    if (await adapter.exists(target)) {
      const raw = await adapter.read(target);
      this.replayHighlightLines(store, raw);
      if (this.highlightStoreError || !raw.trim().startsWith('{"reset":true'))
        throw new Error(`插件内部高光日志损坏或缺少 reset，请从备份恢复：${target}`);
      this.settings.highlightJournal = target;
      this.highlightStore = store;
      if (adapter.getBasePath?.()) this.highlightJournalReady = new Set([path.posix.dirname(target)]);
      this.rebuildHighlightCache();
      return store;
    }
    // 如果配置已经指向私有文件，它却不见了，不能从旧只读库复活曾清空的高光。
    if (this.settings.highlightJournal === target)
      throw new Error(`插件内部高光日志丢失，请从备份恢复：${target}`);
    // 一次性迁移旧 JSON、按文件分片和旧单文件日志；任何读取/解析错误都中止迁移。
    try {
      const legacyPath = this.highlightStorePath();
      if (await adapter.exists(legacyPath)) {
        const parsed = JSON.parse(await adapter.read(legacyPath));
        if (!parsed || !parsed.files || typeof parsed.files !== "object"
            || Array.isArray(parsed.files)) throw new Error("旧库结构无效");
        store.files = parsed.files;
      }
      const dir = this.highlightJournalDir();
      if (await adapter.exists(dir)) {
        const listing = await adapter.list(dir);
        for (const entry of listing.files || []) {
          if (!entry.endsWith(".jsonl")) continue;
          const content = await adapter.read(entry);
          this.replayHighlightLines(store, content, (record) =>
            entry.split("/").pop() === this.highlightJournalName(record.filePath));
        }
      }
      const previous = this.settings.highlightJournal;
      if (await adapter.exists(previous))
        this.replayHighlightLines(store, await adapter.read(previous));
      if (this.highlightStoreError) throw new Error(this.highlightStoreError);
    } catch (error) {
      throw new Error(`旧高光迁移中止，旧数据未改变：${error.message || error}`);
    }
    this.highlightStore = store;
    this.rebuildHighlightCache();
    // 写 reset + 当前有效锚点，并把已关联的图片复制到私有目录；成功后才切换写入。
    await this.migrateHighlightStorage();
    return this.highlightStore;
  }

  replayHighlightLines(store, content, accept = () => true) {
    for (const line of content.split("\n")) {
      if (!line.trim()) continue;
      try {
        const record = JSON.parse(line);
        if (record.reset) { store.files = {}; continue; }
        if (!record.filePath || !accept(record)) continue;
        if (!store.files[record.filePath]) store.files[record.filePath] = [];
        const anchors = store.files[record.filePath];
        const stamp = Number(record.at) > 0 ? Number(record.at) : 0;
        for (const anchor of record.anchor ? [record.anchor] : record.updates || []) {
          if (!anchor?.id) continue;
          const index = anchors.findIndex((a) => a.id === anchor.id);
          const merged = { ...anchor };
          // 派生时间不落盘，所以整对象替换（updates）时必须显式带过来，否则会被
          // 之后的编辑器漂移更新抹掉。
          if (index >= 0) {
            if (merged.createdAt === undefined) merged.createdAt = anchors[index].createdAt;
            if (merged.updatedAt === undefined) merged.updatedAt = anchors[index].updatedAt;
          }
          if (stamp && !merged.createdAt) merged.createdAt = stamp;
          if (index < 0) anchors.push(merged);
          else anchors[index] = merged;
        }
        for (const patch of record.patches || []) {
          const index = anchors.findIndex((a) => a.id === patch.id);
          if (index < 0) continue;
          const merged = { ...anchors[index], ...patch };
          // 只有 patch 能精确表达「这一次改了哪些字段」，整对象快照不行——快照总是
          // 带着 note，拿它判断会把每次漂移更新都算成「修改注释」。
          if (stamp && touchesAnnotation(patch)) merged.updatedAt = stamp;
          anchors[index] = merged;
        }
        if (record.deleted?.length) {
          const deleted = new Set(record.deleted.map((item) => item.id));
          store.files[record.filePath] = anchors.filter((a) => !deleted.has(a.id));
        }
      } catch (_error) {
        this.highlightStoreError = "高光日志含不完整记录（已跳过该行）";
      }
    }
  }

  rebuildHighlightCache() {
    this.highlightById = new Map();
    for (const [filePath, anchors] of Object.entries(this.highlightStore?.files || {}))
      for (const anchor of anchors) if (anchor.id)
        this.highlightById.set(anchor.id, { filePath, anchor });
    // 启动重放或迁移后，旧派生视图不可信；已打开的文件按需重建。
    this.openAnnotationCache?.clear();
    this.syncOpenAnnotationCache();
  }

  // noteLayout 只记录编辑器的行顺序，图片实体仍由 images/imageNames 引用。
  // 旧 JSONL 没有此字段时按旧展示次序（文字在前、图片在后）恢复。
  annotationLayout(anchor) {
    if (typeof anchor.noteLayout === "string") return anchor.noteLayout;
    const markers = (anchor.images || []).map((image, index) =>
      `[图片:${anchor.imageNames?.[index] || path.posix.basename(image)}]`);
    return [anchor.note || "", ...markers].filter(Boolean).join("\n");
  }

  annotationParts(anchor) {
    const names = new Map((anchor.images || []).map((image, index) =>
      [anchor.imageNames?.[index] || path.posix.basename(image), image]));
    const used = new Set();
    const parts = [];
    let text = [];
    const flush = () => { if (text.length) parts.push({ text: text.join("\n") }); text = []; };
    for (const line of this.annotationLayout(anchor).split("\n")) {
      const name = line.trim().match(/^\[图片:([^\]\r\n]+)\]$/)?.[1];
      if (name && names.has(name) && !used.has(name)) {
        flush(); parts.push({ image: names.get(name), name }); used.add(name);
      } else text.push(line);
    }
    flush();
    // 老记录被其它版本直接写 images 时，不能让新图不可见。
    for (const [name, image] of names) if (!used.has(name)) parts.push({ image, name });
    return parts;
  }

  // 注释标题那一行（「注释标题：xxx」）。侧栏卡片排在划线原文下面、悬停预览排在正文
  // 上面，两处共用这一份，风格与卡片里的划线原文一致（同色竖条、小字）。没设标题就
  // 返回 null，调用方据此整行不渲染——没标题的高光不该多出一行空白。
  buildAnnotationTitle(anchor) {
    const title = String(anchor?.title ?? "").trim();
    if (!title) return null;
    const row = document.createElement("div");
    row.className = "nh-annotation-title";
    const label = document.createElement("span");
    label.className = "nh-annotation-title-label";
    label.textContent = "注释标题：";
    const value = document.createElement("span");
    value.className = "nh-annotation-title-value";
    value.textContent = title;
    // 完整标题悬停可读：卡片里限两行、预览框还会整体截断。
    value.title = title;
    row.append(label, value);
    return row;
  }

  // 注释正文的共用渲染：把 noteLayout 拆成文本段与图片段，图片经 Vault 资源取址。
  // 悬停预览与侧栏卡片的注释预览框都走这里，两边排版才会一致。容器自己负责限高。
  // scrollable 决定谁吃滚轮：两个容器都是「限高 + 超出滚动」（悬停预览见 .nh-highlight-preview，
  // 侧栏预览框见 .nh-sidebar-body），滚轮落在框上就该滚框本身。框内没超出上限时下面的处理器
  // 直接放行，滚轮继续滚外层（侧栏、页面），所以传 false 只会让指针停在框上时「滚不动」。
  renderAnnotationBody(container, anchor, { scrollable = true } = {}) {
    container.replaceChildren();
    for (const part of this.annotationParts(anchor)) {
      if (part.image) {
        const src = this.annotationImageResource(part.image);
        if (!src) continue;
        const img = container.ownerDocument.createElement("img");
        img.alt = part.name;
        img.loading = "lazy";
        img.src = src;
        container.appendChild(img);
      } else {
        // Render fenced code as text in <pre>, retaining indentation and never
        // interpreting pasted code as HTML. Ordinary annotation text stays plain.
        const lines = part.text.split("\n");
        let plain = [];
        const appendPlain = () => {
          if (!plain.length) return;
          const text = container.ownerDocument.createElement("div");
          text.textContent = plain.join("\n");
          container.appendChild(text);
          plain = [];
        };
        for (let index = 0; index < lines.length; index += 1) {
          const opening = lines[index].match(/^(`{3,})([A-Za-z0-9_+#.-]*)\s*$/);
          if (!opening) { plain.push(lines[index]); continue; }
          let end = index + 1;
          while (end < lines.length && !new RegExp("^\\s*`{" + opening[1].length + ",}\\s*$").test(lines[end])) end += 1;
          if (end === lines.length) { plain.push(lines[index]); continue; }
          appendPlain();
          const pre = container.ownerDocument.createElement("pre");
          pre.className = "nh-highlight-code-preview";
          const code = container.ownerDocument.createElement("code");
          code.textContent = lines.slice(index + 1, end).join("\n");
          if (opening[2]) code.dataset.language = opening[2];
          pre.appendChild(code);
          container.appendChild(pre);
          index = end;
        }
        appendPlain();
      }
    }
    // 用 onwheel 而不是 addEventListener：属性赋值的处理器一定不是 passive 的，不受
    // wheel 默认 passive 的推断影响；同一个容器若被反复渲染，属性赋值也天然顶掉上一个
    // 处理器，不会叠出一串监听器（叠上就会滚一格跳两格）。赋 null 同样是为了顶掉上一个，
    // 不然同一个容器先当悬停预览、再当侧栏预览时，会留着上一轮的拦截。
    container.onwheel = !scrollable ? null : (wheel) => {
      if (container.scrollHeight <= container.clientHeight) return;
      wheel.preventDefault();
      wheel.stopPropagation();
      container.scrollTop += wheel.deltaY;
    };
  }

  annotationImageResource(imagePath) {
    const file = this.app.vault.getAbstractFileByPath?.(imagePath);
    // 直接写入磁盘的图片尚未被 Vault 索引时，adapter 也能取得资源 URI。
    return file ? this.app.vault.getResourcePath(file)
      : this.app.vault.adapter.getResourcePath?.(imagePath) || "";
  }

  syncOpenAnnotationCache() {
    if (!this.openAnnotationCache || !this.app?.workspace?.iterateAllLeaves) return;
    const opened = new Set();
    this.app.workspace.iterateAllLeaves((leaf) => {
      const file = leaf?.view?.file;
      if (file?.path && /\.(?:md|pdf)$/i.test(file.path)) opened.add(file.path);
    });
    for (const filePath of this.openAnnotationCache.keys())
      if (!opened.has(filePath)) this.openAnnotationCache.delete(filePath);
    for (const filePath of opened) {
      if (this.openAnnotationCache.has(filePath)) continue;
      const notes = new Map();
      for (const anchor of this.highlightStore?.files?.[filePath] || []) {
        if (anchor.id && (anchor.note || anchor.images?.length || anchor.title))
          notes.set(anchor.id, { note: anchor.note || "", images: [...(anchor.images || [])],
            imageNames: [...(anchor.imageNames || [])], title: anchor.title || "",
            noteLayout: this.annotationLayout(anchor) });
      }
      this.openAnnotationCache.set(filePath, notes);
    }
  }

  updateOpenAnnotation(filePath, anchor, id = anchor?.id) {
    const notes = this.openAnnotationCache?.get(filePath);
    if (!notes || !id) return;
    if (!anchor || (!anchor.note && !anchor.images?.length && !anchor.title)) notes.delete(id);
    else notes.set(id, { note: anchor.note || "", images: [...(anchor.images || [])],
      imageNames: [...(anchor.imageNames || [])], title: anchor.title || "",
      noteLayout: this.annotationLayout(anchor) });
    if (this.hoverHighlightId === id) {
      this.hideHighlightPreview();
      this.hoverHighlightId = null;
    }
  }

  annotationForHighlight(id) {
    const found = this.findHighlight(id);
    if (!found) return null;
    const notes = this.openAnnotationCache?.get(found.filePath);
    return notes ? notes.get(id) || null : found.anchor;
  }

  storagePaths() {
    return { journal: this.settings.highlightJournal };
  }

  // 8 个固定色，规则表不再随数据变化：整张表就是 HIGHLIGHT_COLOR_DEFS 的展开。
  // 保留「生成一次 + 记下已覆盖的色值」这套形状，renderEntries 的覆盖检查不用改。
  applyHighlightPalette() {
    if (typeof document === "undefined" || !document.head) return;
    const rules = [];
    for (const key of HIGHLIGHT_COLORS) {
      const hex = HIGHLIGHT_COLOR_DEFS[key].hex;
      const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16));
      rules.push(`.nh-highlight-swatches .nh-hl-${key} { background: ${hex}; }`);
      rules.push(`::highlight(nh-hl-${key}), .cm-content .nh-hl-${key} { background-color: rgba(${r}, ${g}, ${b}, 0.66); }`);
      // 半透明而非实色：亮/暗两种主题下文字都还读得清，也就不必反过来改文字颜色。
      rules.push(`.theme-dark .nh-sidebar-card.nh-hl-${key} { --nh-card-tint: rgba(${r}, ${g}, ${b}, 0.16); --nh-card-line: rgba(${r}, ${g}, ${b}, 0.38); }`);
      rules.push(`.theme-light .nh-sidebar-card.nh-hl-${key} { --nh-card-tint: rgba(${r}, ${g}, ${b}, 0.20); --nh-card-line: rgba(${r}, ${g}, ${b}, 0.45); }`);
    }
    if (!this.highlightPaletteStyle) {
      this.highlightPaletteStyle = document.createElement("style");
      this.highlightPaletteStyle.id = "nh-highlight-palette";
      document.head.appendChild(this.highlightPaletteStyle);
    }
    this.highlightPaletteStyle.textContent = rules.join("\n");
    this.highlightPaletteStyle.dataset.fixed = "1";
    this.highlightPaletteHexes = new Set(Object.values(DEFAULT_HIGHLIGHT_PALETTE));
  }

  // 2.3.x 的自定义调色板入口。颜色从 2.4.0 起固定为 8 种，桌面端可能还按老版本调用，
  // 这里保留路径不报错、也不再写盘，直接回固定表——否则旧桌面端会收到 404 并弹出
  // 「运行版本较旧，请重载 2.3.8」那句误导提示。
  async configureHighlightColors() {
    this.applyHighlightPalette();
    return { ...DEFAULT_HIGHLIGHT_PALETTE };
  }

  // 单条导出目标由桌面端配置页推下来：索引文档（Vault 内 .md 相对路径）、注释目录
  // （Vault 内目录）。空串表示「还没配」。索引文档的标题不再单独配置——新建时用文档
  // 自己的文件名，每条记录用注释标题、没有就取原文前几个字（见 annotationTitle）。
  async configureHighlightSingleExport(payload) {
    const raw = payload && typeof payload === "object" ? payload : {};
    const indexPath = String(raw.index_path || "").trim();
    const noteDir = String(raw.note_dir || "").trim();
    const config = {
      indexPath: indexPath ? this.checkedStoragePath(indexPath, ".md") : "",
      noteDir: noteDir ? this.checkedVaultDirectory(noteDir) : "",
      ...(raw.vault_path ? { vaultPath: this.checkedExportVault(raw.vault_path) } : {}),
    };
    if (config.vaultPath) {
      if (config.indexPath) this.checkedExportLocalPath(config.vaultPath, config.indexPath);
      if (config.noteDir) this.checkedExportLocalPath(config.vaultPath, config.noteDir);
    }
    const next = { ...this.settings, highlightSingleExport: config };
    await this.saveData(next);
    this.settings = next;
    return config;
  }

  // 悬浮窗上摆哪 3 种颜色。桌面端给的不是 3 个合法键就 400，不静默改成别的——
  // 静默回退会让配置页显示的和实际生效的对不上。
  async configureHighlightActiveColors(payload) {
    const raw = payload && typeof payload === "object" ? payload : {};
    const list = Array.isArray(raw.colors) ? raw.colors : [];
    const keys = list.map((item) => normalizeHighlightColor(item));
    if (keys.length !== ACTIVE_COLOR_COUNT || keys.some((key) => !key)
        || new Set(keys).size !== keys.length)
      throw new Error(`悬浮窗颜色必须是 ${ACTIVE_COLOR_COUNT} 个互不相同的颜色。`);
    const next = { ...this.settings, nativeHighlightColors: keys };
    await this.saveData(next);
    this.settings = next;
    return keys;
  }

  // 悬浮窗（划选色盘）和右键菜单的「切换颜色」都读这一个：8 色表里的 3 个键。
  activeHighlightColors() {
    return normalizeActiveColors(this.settings?.nativeHighlightColors);
  }

  checkedVaultDirectory(input) {
    const raw = String(input || "");
    if (!raw || raw.includes("\\") || raw.includes("\0") || raw.startsWith("/")
        || /^[A-Za-z]:/.test(raw) || raw.split("/").some((part) => part === ".." || part === "."))
      throw new Error("注释目录必须是 Vault 内不含 . / .. 的相对路径。");
    const normalized = path.posix.normalize(raw);
    if (normalized === raw && normalized.split("/").every(Boolean)) return raw;
    throw new Error("注释目录不能包含空目录、. 或 ..。");
  }

  checkedStoragePath(input, suffix = ".jsonl") {
    const raw = String(input || "");
    if (!raw || raw.includes("\\") || raw.includes("\0") || raw.startsWith("/")
        || /^[A-Za-z]:/.test(raw) || raw.split("/").includes("..")
        || raw.split("/").includes(".") || !raw.endsWith(suffix))
      throw new Error(`必须是 Vault 内的 ${suffix} 相对路径。`);
    const normalized = path.posix.normalize(raw);
    if (normalized === raw && normalized.split("/").every(Boolean)) return raw;
    throw new Error("存储路径不能包含空目录、. 或 ..。");
  }

  async writeSnapshot(target, text) {
    const base = this.app.vault.adapter.getBasePath?.();
    if (base) {
      const destination = path.join(base, ...target.split("/"));
      await fs.promises.mkdir(path.dirname(destination), { recursive: true });
      const temp = `${destination}.nh-${crypto.randomBytes(8).toString("hex")}.tmp`;
      try {
        await fs.promises.writeFile(temp, text, { encoding: "utf8", flag: "wx" });
        await fs.promises.rename(temp, destination);
      } finally { await fs.promises.unlink(temp).catch(() => {}); }
    } else {
      await this.app.vault.adapter.mkdir?.(path.posix.dirname(target));
      await this.app.vault.adapter.write(target, text);
    }
  }

  activeHighlightCount() {
    return Object.values(this.highlightStore?.files || {})
      .reduce((total, anchors) => total + anchors.length, 0);
  }

  // 桌面端「按颜色导出」菜单要的东西：总数 + 每个固定色的条数。桶用 highlightColorKey，
  // 与导出分档用的是同一个判断，所以 Σ(各色条数) 恒等于 total —— 菜单上的数字不会
  // 和真正导出来的条数对不上。颜色顺序固定按 HIGHLIGHT_COLORS。
  highlightExportOptions() {
    const counts = new Map(HIGHLIGHT_COLORS.map((key) => [key, 0]));
    let total = 0;
    for (const anchors of Object.values(this.highlightStore?.files || {})) {
      for (const anchor of anchors) {
        total += 1;
        counts.set(highlightColorKey(anchor), (counts.get(highlightColorKey(anchor)) || 0) + 1);
      }
    }
    return {
      total,
      colors: HIGHLIGHT_COLORS.map((key) => ({
        key,
        label: HIGHLIGHT_COLOR_DEFS[key].label,
        colorHex: HIGHLIGHT_COLOR_DEFS[key].hex,
        dot: HIGHLIGHT_COLOR_DEFS[key].dot,
        count: counts.get(key) || 0,
      })),
      single_export: this.settings.highlightSingleExport || { indexPath: "", noteDir: "", title: "" },
    };
  }

  async backupHighlightJournal(target) {
    const base = this.app.vault.adapter.getBasePath?.();
    if (!base) throw new Error("清空高光仅支持本地 Vault，不能无备份覆盖日志。");
    const source = path.join(base, ...target.split("/"));
    const backupDir = path.join(base, ...this.manifest.dir.split("/"), "backup");
    const backupName = `highlight-before-reset-${Date.now()}-${crypto.randomBytes(5).toString("hex")}.jsonl`;
    await fs.promises.mkdir(backupDir, { recursive: true });
    const destination = path.join(backupDir, backupName);
    try { await fs.promises.copyFile(source, destination, fs.constants.COPYFILE_EXCL); }
    catch (error) { if (error.code !== "ENOENT") throw error; return ""; }
    return `${this.manifest.dir}/backup/${backupName}`;
  }

  // 一次性清空活动高光。覆盖后的 JSONL 必须留一行 reset，阻止旧只读库重放复活。
  // 写盘成功之前绝不修改内存/装饰；调用者先独立备份原日志。
  async resetHighlights(expectedCount) {
    if (this.highlightResetting) throw new Error("高光清空正在进行，请稍候。");
    const target = this.checkedStoragePath(this.settings?.highlightJournal);
    if (!Number.isSafeInteger(expectedCount) || expectedCount < 0)
      throw new Error("清空时必须提供诊断中的有效高光数量。");
    this.highlightResetting = true;
    try {
      // 已在途的提交先结束；新提交由 applyHighlight/addAnchor/changeHighlight 拒绝。
      await Promise.all([...this.highlightInFlight?.values() || []]
        .map((operation) => operation.catch(() => {})));
      await this.flushAnchorEdits({ allowDuringReset: true });
      if (this.highlightWriteTail) await this.highlightWriteTail;
      if (this.settings.highlightJournal !== target)
        throw new Error("高光数据文件位置已改变，请重新读取数量并确认。");
      const store = await this.loadHighlights();
      const count = this.activeHighlightCount();
      if (count !== expectedCount)
        throw new Error(`高光数量已变化（现在 ${count} 条），请重新确认后再清空。`);
      const backup = await this.backupHighlightJournal(target);
      const operation = this.writeSnapshot(target,
        `${JSON.stringify({ reset: true, at: Date.now() })}\n`);
      this.highlightWriteTail = operation;
      try { await operation; }
      catch (error) {
        // 失败后内存仍是旧库；允许修复磁盘问题后重试，不留下永远 rejected 的尾巴。
        if (this.highlightWriteTail === operation) this.highlightWriteTail = Promise.resolve();
        throw error;
      }
      store.files = {};
      this.rebuildHighlightCache();
      this.pendingAnchorEdits?.clear();
      this.deletingHighlights?.clear();
      this.captures?.clear();
      this.hoverHighlightId = null;
      this.hideHighlightPreview();
      this.closeHighlightMenu();
      this.interactiveRanges = [];
      this.scheduleHighlightSidebarRefresh?.();
      // 绘制故障只记诊断，不把已落盘的清空误报为失败导致用户重复操作。
      try { this.refreshEditorDecorations(); }
      catch (error) { this.lastEditorError = `清空后编辑器重画：${error.message || error}`; }
      try { this.refreshHighlights(); }
      catch (error) { this.lastRangeError = `清空后浮层重画：${error.message || error}`; }
      return { removed: count, active_highlights: 0, journal: target, backup };
    } finally {
      this.highlightResetting = false;
    }
  }

  async migrateHighlightStorage() {
    if (this.highlightResetting) throw new Error("高光清空期间不能迁移数据文件。");
    const target = this.fixedHighlightJournal || `${this.manifest.dir}/storage/highlights.jsonl`;
    await this.flushAnchorEdits();
    const previousWrite = this.highlightWriteTail || Promise.resolve();
    const operation = previousWrite.catch(() => {}).then(async () => {
      if (this.highlightResetting) throw new Error("高光清空期间不能迁移数据文件。");
      if (target === this.settings.highlightJournal) return this.storagePaths();
      if (await this.app.vault.adapter.exists?.(target))
        throw new Error(`目标 ${target} 已存在；为防止覆盖，先选一个新文件名。`);
      // 锁住追加队列；若注释图片已有路径，复制到新 JSONL 对应的 image 目录。
      // 旧图片留作历史备份，提交前失败则只清理本次新复制的图片。
      const oldJournal = this.settings.highlightJournal;
      const oldImageDir = this.highlightImageDirectory();
      const newImageDir = path.posix.join(path.posix.dirname(target),
        `image${path.posix.basename(target, ".jsonl")}`);
      const copies = [];
      let snapshotUncommitted = false;
      const moved = new Map();
      const copiedImages = new Map();
      const base = this.app.vault.adapter.getBasePath?.();
      try {
        const rows = [{ reset: true, at: Date.now() }];
        for (const [filePath, anchors] of Object.entries(this.highlightStore.files)) {
          for (const anchor of anchors) {
            if (!anchor.id) continue;
            const images = [];
            for (const image of anchor.images || []) {
              if (image.startsWith(`${newImageDir}/`)) { images.push(image); continue; }
              if (!base || typeof image !== "string" || image.startsWith("/")
                  || image.includes("\\") || image.split("/").some((part) => !part || part === "." || part === ".."))
                throw new Error("旧注释图片路径无效或当前 Vault 不支持本地迁移。");
              if (!copiedImages.has(image)) {
                const name = `${crypto.createHash("sha256").update(image).digest("hex").slice(0, 12)}-${path.posix.basename(image)}`;
                const destination = `${newImageDir}/${name}`;
                const sourceFile = path.join(base, ...image.split("/"));
                const targetFile = path.join(base, ...destination.split("/"));
                await fs.promises.mkdir(path.dirname(targetFile), { recursive: true });
                await fs.promises.copyFile(sourceFile, targetFile, fs.constants.COPYFILE_EXCL);
                copies.push(targetFile);
                copiedImages.set(image, destination);
              }
              images.push(copiedImages.get(image));
            }
            const nextAnchor = { ...anchor, ...(anchor.images ? { images } : {}) };
            moved.set(anchor.id, nextAnchor);
            rows.push({ filePath, anchor: nextAnchor });
          }
        }
        await this.writeSnapshot(target, rows.map((row) => JSON.stringify(row)).join("\n") + "\n");
        snapshotUncommitted = true;
        const previous = this.settings;
        this.settings = { ...previous, highlightJournal: target };
        try { await this.saveData(this.settings); }
        catch (error) { this.settings = previous; throw error; }
        snapshotUncommitted = false;
        for (const anchors of Object.values(this.highlightStore.files)) {
          for (let i = 0; i < anchors.length; i += 1)
            if (moved.has(anchors[i].id)) anchors[i] = moved.get(anchors[i].id);
        }
        this.rebuildHighlightCache();
        this.highlightJournalReady = new Set([path.posix.dirname(target)]);
        // 当前数据已在私有库落盘；历史原日志/图片挪进插件备份，避免留在笔记区。
        if (base) {
          const backupDir = path.join(base, ...this.manifest.dir.split("/"), "backup");
          const label = `highlight-migration-${Date.now()}-${crypto.randomBytes(4).toString("hex")}`;
          try {
            await fs.promises.mkdir(backupDir, { recursive: true });
            for (const [relative, suffix] of [[oldJournal, ".jsonl"], [oldImageDir, "-images"]]) {
              if (relative === target || relative === newImageDir) continue;
              const source = path.join(base, ...relative.split("/"));
              try { await fs.promises.rename(source, path.join(backupDir, label + suffix)); }
              catch (error) { if (error.code !== "ENOENT") throw error; }
            }
          } catch (error) {
            this.lastMigrationWarning = `迁移已完成，但旧资料归档失败：${error.message || error}`;
            new Notice(this.lastMigrationWarning);
          }
        }
      } catch (error) {
        if (snapshotUncommitted) {
          if (base) await fs.promises.unlink(path.join(base, ...target.split("/"))).catch(() => {});
          else if (this.app.vault.adapter.remove)
            await this.app.vault.adapter.remove(target).catch(() => {});
        }
        for (const copy of copies) await fs.promises.unlink(copy).catch(() => {});
        throw error;
      }
      return this.storagePaths();
    });
    this.highlightWriteTail = operation;
    return operation;
  }

  highlightImageDirectory() {
    const journal = this.settings.highlightJournal;
    return path.posix.join(path.posix.dirname(journal),
      `image${path.posix.basename(journal, ".jsonl")}`);
  }

  async storeHighlightImage(file) {
    const types = { "image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
      "image/webp": ".webp", "image/bmp": ".bmp" };
    const ext = types[file?.type];
    if (!ext || file.size > 10 * 1024 * 1024 || !file.size)
      throw new Error("注释图片须为 PNG/JPG/GIF/WEBP/BMP，单张不超过 10 MiB。");
    const base = this.app.vault.adapter.getBasePath?.();
    if (!base) throw new Error("注释图片仅支持本地 Obsidian 仓库。");
    const folder = this.highlightImageDirectory();
    const name = `image-${crypto.randomBytes(12).toString("hex")}${ext}`;
    const absolute = path.join(base, ...folder.split("/"), name);
    await fs.promises.mkdir(path.dirname(absolute), { recursive: true });
    await fs.promises.writeFile(absolute, Buffer.from(await file.arrayBuffer()), { flag: "wx" });
    return `${folder}/${name}`;
  }

  async removeUncommittedHighlightImage(relative) {
    const base = this.app.vault.adapter.getBasePath?.();
    if (!base || !relative.startsWith(`${this.highlightImageDirectory()}/`)) return;
    await fs.promises.unlink(path.join(base, ...relative.split("/"))).catch(() => {});
  }

  // 一个导出根目录，底下两棵树：索引/<文档名>__<扩展名>.md 与
  // 注释/<文档名>__<扩展名>/<注释名>/注释.md。**不镜像来源的父目录**——来源本来放在
  // 文档资料库/<哈希>/ 里，镜像出来只是给导出树套一层没有信息量的中间层；文档名与
  // 扩展名已经够区分（讲义.md 与 讲义.pdf 分属 讲义__md 与 讲义__pdf）。真遇到两份
  // 同名同扩展名的来源（不同目录各放一个 a.pdf），按路径排序补 -2、-3，不合并成一篇。
  // 注释目录名取自定义注释标题，没设标题就用原文的前几个字（见 annotationFolderName），
  // 图片复制在该注释目录下，导出不依赖私有图片库。
  // `color` 为空导出全部；给一个颜色键就只导那一档，整体下沉到 `<目录>/高光-<色名>/`。
  // 两档各自一个清单文件，靠 folder 天然隔离，重导一档不会清理另一档的产物。
  exportHighlights(directory, { color = "", crossVault = false } = {}) {
    const scope = color ? `高光-${HIGHLIGHT_COLOR_DEFS[color].label}` : "所有高光";
    const folder = this.checkedStoragePath(`${String(directory || "").replace(/\/$/, "")}/${scope}/index.md`, ".md")
      .slice(0, -"/index.md".length);
    const link = (id) => `obsidian://noting-helper-highlight?vault=${encodeURIComponent(this.app.vault.getName())}&id=${encodeURIComponent(id)}`;
    const safe = (text) => String(text ?? "").replace(/\r/g, "").replace(/[\[\]\|]/g, "\\$&");
    const marker = "> 此文件由 NotingHelper 导出；重新导出会覆盖它。不要在此编辑实时高光。";
    const files = [], images = [];
    const documentNames = new Set();
    let count = 0;
    for (const [source, all] of Object.entries(this.highlightStore.files).sort(([a], [b]) => a.localeCompare(b))) {
      // 分档过滤放在建索引之前：本档里没有高光的来源文档不生成空索引，否则每导一次
      // 都会给整棵树多出一批空文件，再导一次又被清单清掉。
      const anchors = all.filter((anchor) => !color || highlightColorKey(anchor) === color);
      if (!anchors.length) continue;
      const extension = path.posix.extname(source).slice(1).toLowerCase();
      const key = uniqueDirectoryName(
        `${path.posix.basename(source, path.posix.extname(source))}__${extension}`, documentNames);
      const indexPath = `${folder}/索引/${key}.md`;
      const noteRoot = `${folder}/注释/${key}`;
      // 分档导出整篇只有一种颜色，标题里带上颜色名和圆点；「所有高光」是混色的，
      // 颜色标在每一条记录上。
      const heading = color
        ? `# ${safe(key)} · 高光索引（${HIGHLIGHT_COLOR_DEFS[color].label} ${HIGHLIGHT_COLOR_DEFS[color].dot}）`
        : `# ${safe(key)} · 高光索引`;
      const index = [heading, "", marker, "",
        `- 来源：${this.highlightExportSourceLink(source, crossVault)}`, ""];
      const noteNames = new Set();
      for (const anchor of [...anchors].sort((a, b) => (a.createdAt || 0) - (b.createdAt || 0))) {
        if (!anchor.id) continue;
        const id = anchor.id;
        // 目录名来自标题或原文，符号已在 annotationFolderName 里清掉，不会路径穿越；
        // 同一文档内重名再补 -2、-3，保证一条注释一个目录。
        const noteDir = `${noteRoot}/${uniqueDirectoryName(annotationFolderName(anchor), noteNames)}`;
        const notePath = `${noteDir}/注释.md`;
        count += 1;
        index.push(`## ${safe(anchor.exact || id)}`, "",
          `- 高光 ID：${safe(id)}`,
          `- 颜色：${anchorColorLabel(anchor)} ${anchorColorDot(anchor)}`,
          `- [定位到高光](${link(id)})`);
        if (anchor.note || anchor.images?.length || anchor.tags?.length) {
          const notes = [`# ${safe(key)} · ${safe(id)}`, "", marker, "",
            `- 来源：${this.highlightExportSourceLink(source, crossVault)}`,
            `- [定位到高光](${link(id)})`];
          const tagLine = tagMarkup(anchor.tags);
          // 「标签：」后面这个空格不能省：行内标签的 `#` 前面必须是空白或行首，
          // 紧跟全角冒号的话 Obsidian 不把它当标签。
          if (tagLine) notes.push(`- 标签： ${tagLine}`);
          notes.push("");
          let imageIndex = 0;
          for (const part of this.annotationParts(anchor)) {
            if (!part.image) { notes.push(part.text); continue; }
            const name = `${++imageIndex}-${path.posix.basename(part.image)}`;
            const destination = `${noteDir}/image/${name}`;
            images.push({ source: part.image, target: destination });
            notes.push(`![${safe(part.name)}](image/${name})`);
          }
          files.push({ path: notePath, text: notes.join("\n") + "\n" });
          index.push(`- [[${safe(notePath.slice(0, -3))}|查看注释]]`);
        }
        index.push("");
      }
      files.push({ path: indexPath, text: index.join("\n") + "\n" });
    }
    return { folder, files, images, count, color, scope };
  }

  async writeHighlightExport(directory, options = {}) {
    await this.flushAnchorEdits();
    const destination = this.highlightExportDestination(options.vaultPath || "");
    const result = this.exportHighlights(directory, { ...options, crossVault: destination.crossVault });
    const manifestPath = `${result.folder}/.noting-helper-export.json`;
    const adapter = destination.adapter;
    let previous = { files: [], images: [] };
    if (await adapter.exists?.(manifestPath)) {
      previous = JSON.parse(await adapter.read(manifestPath));
      if (!Array.isArray(previous.files) || !Array.isArray(previous.images))
        throw new Error("高光导出清单损坏，请先备份并修复，不覆盖现有目录。");
    }
    const oldImages = new Set(previous.images);
    const marker = "> 此文件由 NotingHelper 导出；";
    for (const entry of result.files) {
      if (await adapter.exists?.(entry.path)) {
        const existing = await adapter.read(entry.path);
        if (!existing.split("\n")[2]?.startsWith(marker))
          throw new Error(`${entry.path} 不是本插件生成的文档，拒绝覆盖。`);
      }
    }
    const base = adapter.getBasePath?.();
    for (const image of result.images) {
      if (!base) throw new Error("导出注释图片需要本地 Vault。");
      this.checkedExportLocalPath(base, image.target);
      // 已导出的同一路径由插件清单拥有，可更新图片内容；其它文件不覆盖。
      if (!oldImages.has(image.target) && await adapter.exists?.(image.target))
        throw new Error(`${image.target} 已存在，拒绝覆盖图片。`);
      await this.copySingleExportImage(image.source, image.target, base);
    }
    for (const entry of result.files) await destination.write(entry.path, entry.text);
    const current = new Set([...result.files.map(({ path }) => path),
      ...result.images.map(({ target }) => target)]);
    // 只删除先前清单内属于此导出根的旧产物，绝不碰用户来源或未知文件。
    const emptiedParents = new Set();
    for (const stale of [...previous.files, ...previous.images]) {
      if (typeof stale !== "string" || !stale.startsWith(`${result.folder}/`)
          || stale.split("/").includes("..") || current.has(stale)) continue;
      if (base) await fs.promises.unlink(this.checkedExportLocalPath(base, stale)).catch((error) => {
        if (error.code !== "ENOENT") throw error;
      });
      else await adapter.remove?.(stale);
      emptiedParents.add(path.posix.dirname(stale));
    }
    // 注释目录名取自标题或原文，改一次标题就换一个目录，只回收文件会在导出树里留下
    // 一串空目录。rmdir 只在目录为空时成功，所以不会碰到任何用户内容；从最深的目录
    // 往上走，删到非空目录就停（再往上必然也非空）。深目录排在前面。
    if (base) for (const parent of [...emptiedParents].sort((a, b) => b.length - a.length)) {
      for (let current = parent; current.startsWith(`${result.folder}/`);) {
        const empty = await fs.promises
          .rmdir(this.checkedExportLocalPath(base, current)).then(() => true, () => false);
        if (!empty) break;
        current = path.posix.dirname(current);
      }
    }
    await destination.write(manifestPath, JSON.stringify({
      files: result.files.map(({ path }) => path),
      images: result.images.map(({ target }) => target),
    }, null, 2) + "\n");
    return { index: `${result.folder}/索引`, notes: `${result.folder}/注释`,
      count: result.count, folder: result.folder, color: result.color, scope: result.scope,
      ...(options.vaultPath ? { vault_path: options.vaultPath } : {}) };
  }

  // 把**一条**高光并进用户自己维护的索引文档。和批量导出的根本差别：
  //   - 目标文档是用户的（很可能是那份问题记录），所以绝不走 writeHighlightExport 的
  //     「第 2 行不是本插件 marker 就拒绝覆盖」，也不维护清单、不删任何东西；
  //   - 只动属于这个高光 ID 的那一段，用户其它内容原样保留。
  checkedExportVault(input) {
    const raw = String(input || "");
    if (!path.isAbsolute(raw) || raw.includes("\0"))
      throw new Error("导出仓库必须是本地绝对路径。");
    if (!fs.statSync(path.join(raw, ".obsidian")).isDirectory())
      throw new Error("导出目录不属于 Obsidian 仓库（缺少 .obsidian）。");
    return raw;
  }

  checkedExportLocalPath(base, relative) {
    const raw = String(relative || "");
    if (!raw || raw.includes("\\") || raw.includes("\0") || path.posix.isAbsolute(raw)
        || /^[A-Za-z]:/.test(raw) || raw.split("/").some(p => !p || p === "." || p === ".."))
      throw new Error("导出路径必须位于所选仓库内。");
    const root = fs.realpathSync(base);
    const target = path.resolve(root, ...raw.split("/"));
    let ancestor = target;
    while (!fs.existsSync(ancestor) && ancestor !== root) ancestor = path.dirname(ancestor);
    const actual = fs.realpathSync(ancestor);
    const inside = path.relative(root, actual);
    if (inside === ".." || inside.startsWith(`..${path.sep}`) || path.isAbsolute(inside))
      throw new Error("导出路径通过链接越过了仓库边界。");
    return target;
  }

  highlightExportDestination(vaultPath = "") {
    const sourceBase = this.app.vault.adapter.getBasePath?.();
    const crossVault = !!vaultPath && (!sourceBase || path.resolve(sourceBase) !== path.resolve(vaultPath));
    if (!crossVault) return { adapter: this.app.vault.adapter, base: sourceBase,
      crossVault: false, write: (target, text) => this.writeSnapshot(target, text) };
    const base = this.checkedExportVault(vaultPath);
    const local = relative => this.checkedExportLocalPath(base, relative);
    const adapter = {
      getBasePath: () => base,
      exists: async relative => fs.existsSync(local(relative)),
      read: async relative => fs.promises.readFile(local(relative), "utf8"),
    };
    return { adapter, base, crossVault: true, local,
      write: async (relative, text) => {
        const target = local(relative);
        await fs.promises.mkdir(path.dirname(target), { recursive: true });
        local(relative); // Recheck after creating any missing parents.
        const temp = `${target}.nh-${crypto.randomBytes(8).toString("hex")}.tmp`;
        try {
          await fs.promises.writeFile(temp, text, { encoding: "utf8", flag: "wx" });
          await fs.promises.rename(temp, target);
        } finally { await fs.promises.unlink(temp).catch(() => {}); }
      } };
  }

  highlightExportSourceLink(filePath, crossVault = false) {
    const safe = value => String(value).replace(/[\[\]\|]/g, "\\$&");
    if (!crossVault) return `[[${safe(filePath)}]]`;
    return `[${safe(filePath)}](obsidian://open?vault=${encodeURIComponent(this.app.vault.getName())}&file=${encodeURIComponent(filePath)})`;
  }

  async exportSingleHighlight(id) {
    await this.flushAnchorEdits();
    const found = this.findHighlight(id);
    if (!found) throw new Error("这条高光已不存在。");
    const config = this.settings.highlightSingleExport || {};
    if (!config.indexPath || !config.noteDir)
      throw new Error("尚未配置单条导出目标（索引文档与注释目录）。");
    const { filePath, anchor } = found;
    this.checkedStoragePath(config.indexPath, ".md");
    this.checkedVaultDirectory(config.noteDir);
    const destination = this.highlightExportDestination(config.vaultPath || "");
    const adapter = destination.adapter;

    // 注释先行：索引里要链接到它，索引写失败时最多留一个没被引用的注释目录，
    // 反过来则会留下一条指向不存在文件的链接。
    const noteDir = await this.singleExportNoteDir(config.noteDir, anchor, adapter);
    const notePath = `${noteDir}/注释.md`;
    const link = (value) => `obsidian://noting-helper-highlight?vault=${encodeURIComponent(this.app.vault.getName())}&id=${encodeURIComponent(value)}`;
    const safe = (text) => String(text ?? "").replace(/\r/g, "").replace(/[\[\]\|]/g, "\\$&");
    // 标题和注释目录名同源（见 annotationTitle）：设了注释标题就用它，否则截原文前 24 字。
    const heading = annotationTitle(anchor, 24) || id;
    const marker = "> 此文件由 NotingHelper 导出；重新导出会覆盖它。不要在此编辑实时高光。";
    const notes = [`# ${safe(heading)}`, "", marker, "",
      `- 来源：${this.highlightExportSourceLink(filePath, destination.crossVault)}`,
      `- [定位到高光](${link(id)})`];
    const tagLine = tagMarkup(anchor.tags);
    // 「标签：」后面这个空格不能省：行内标签的 `#` 前面必须是空白或行首，
    // 紧跟全角冒号的话 Obsidian 不把它当标签。
    if (tagLine) notes.push(`- 标签： ${tagLine}`);
    notes.push("");
    let imageIndex = 0;
    for (const part of this.annotationParts(anchor)) {
      if (!part.image) { notes.push(part.text); continue; }
      const name = `${++imageIndex}-${path.posix.basename(part.image)}`;
      await this.copySingleExportImage(part.image, `${noteDir}/image/${name}`, destination.base);
      notes.push(`![${safe(part.name)}](image/${name})`);
    }
    await destination.write(notePath, notes.join("\n") + "\n");

    const block = [
      `## ${safe(heading)}`, "",
      `- 高光 ID：${safe(id)}`,
      `- 颜色：${anchorColorLabel(anchor)} ${anchorColorDot(anchor)}`,
      `- 来源：${this.highlightExportSourceLink(filePath, destination.crossVault)}`,
      `- [定位到高光](${link(id)})`,
      `- [[${safe(notePath.slice(0, -3))}|查看注释]]`,
    ].join("\n");
    const existing = await adapter.exists?.(config.indexPath)
      ? await adapter.read(config.indexPath) : "";
    const eol = existing.includes("\r\n") ? "\r\n" : "\n";
    // 新建时的一级标题就是这个索引文档自己的文件名，不再单独配一个「导出标题」。
    const docTitle = path.posix.basename(config.indexPath, ".md") || "高光索引";
    const merged = existing
      ? mergeIndexRecord(existing, id, block, eol)
      : `# ${safe(docTitle)}\n\n${block}\n`;
    await destination.write(config.indexPath, merged);
    return { index: config.indexPath, note: notePath, id, ...(config.vaultPath ? { vault_path: config.vaultPath } : {}) };
  }

  // 注释目录：同名目录里已经躺着一份**不是我们生成的**注释.md 时改名 -2、-3。
  // 批量导出遇到这种撞车是抛错（整棵树归它管），单条导出必须成功——用户的问题
  // 笔记区里同名目录太常见了，换一个不撞的名字就行。
  async singleExportNoteDir(root, anchor, adapter = this.app.vault.adapter) {
    const base = annotationFolderName(anchor);
    for (let index = 1; index < 100; index += 1) {
      const candidate = index === 1 ? `${root}/${base}` : `${root}/${base}-${index}`;
      const target = `${candidate}/注释.md`;
      if (!await adapter.exists?.(target)) return candidate;
      const existing = await adapter.read(target);
      if (existing.split("\n")[2]?.startsWith("> 此文件由 NotingHelper 导出；")) return candidate;
    }
    throw new Error(`注释目录名 ${base} 已被占用，请清理后重试。`);
  }

  // 只往上面的 singleExportNoteDir 选定的目录里写：那个目录已经确认要么是新建的、
  // 要么里面是我们自己生成的注释，所以图片直接覆盖同名文件，不会碰到用户的东西。
  async copySingleExportImage(source, target, targetBase = this.app.vault.adapter.getBasePath?.()) {
    const sourceBase = this.app.vault.adapter.getBasePath?.();
    if (!sourceBase || !targetBase) throw new Error("导出注释图片需要本地 Vault。");
    const sourceFile = this.checkedExportLocalPath(sourceBase, source);
    const destination = this.checkedExportLocalPath(targetBase, target);
    await fs.promises.mkdir(path.dirname(destination), { recursive: true });
    this.checkedExportLocalPath(targetBase, target);
    await fs.promises.copyFile(sourceFile, destination);
  }

  async appendAnchorRecord(filePath, anchor) {
    return this.appendHighlightRecord(filePath, { anchor });
  }

  async appendHighlightRecord(filePath, record) {
    const target = this.settings?.highlightJournal
      || `${this.highlightJournalDir()}/${this.highlightJournalName(filePath)}`;
    const dir = path.posix.dirname(target);
    const name = path.posix.basename(target);
    const line = `${JSON.stringify({ filePath, ...record })}\n`;
    const base = this.app.vault.adapter.getBasePath?.();
    if (base) {
      // Obsidian 失焦时 renderer 的 Node 异步文件回调可能被推迟约一秒；
      // 一条 JSONL 只追加几百字节，同步完成后才允许标记可见，
      // 不引入「先显示但还不能编辑」的临时状态。仅限桌面本地 adapter。
      const directory = path.join(base, ...dir.split("/"));
      let mkdirMs = 0;
      const ensureDirectory = () => {
        if (this.highlightJournalReady?.has(dir)) return;
        const started = performance.now();
        fs.mkdirSync(directory, { recursive: true });
        mkdirMs += performance.now() - started;
        if (!this.highlightJournalReady) this.highlightJournalReady = new Set();
        this.highlightJournalReady.add(dir);
      };
      ensureDirectory();
      const appendStartedAt = performance.now();
      try {
        fs.appendFileSync(path.join(directory, name), line, "utf8");
      } catch (error) {
        if (error.code !== "ENOENT") throw error;
        this.highlightJournalReady?.delete(dir);
        ensureDirectory();
        fs.appendFileSync(path.join(directory, name), line, "utf8");
      }
      this.lastHighlightMkdirMs = Math.round(mkdirMs * 10) / 10;
      this.lastHighlightAppendMs = Math.round((performance.now() - appendStartedAt) * 10) / 10;
    } else {
      // 测试 adapter；单文件也保持追加日志语义。
      await this.app.vault.adapter.mkdir?.(dir);
      let existing = "";
      try { existing = await this.app.vault.adapter.read(target); } catch (_error) {}
      await this.app.vault.adapter.write(target, existing + line);
    }
  }

  scheduleAnchorEditFlush(delay = 200) {
    if (this.anchorEditFlushTimer) clearTimeout(this.anchorEditFlushTimer);
    this.anchorEditFlushTimer = setTimeout(() => {
      this.anchorEditFlushTimer = null;
      this.flushAnchorEdits().catch((error) => {
        this.lastAnchorEditError = `高光更新落盘失败：${error.message || error}`;
        if ((this.anchorEditRetries || 0) < 3) {
          this.anchorEditRetries = (this.anchorEditRetries || 0) + 1;
          this.scheduleAnchorEditFlush(500 * (2 ** (this.anchorEditRetries - 1)));
        } else new Notice(this.lastAnchorEditError);
      });
    }, delay);
  }

  queueAnchorEdit(filePath, anchor, id) {
    if (this.highlightResetting) return;
    this.pendingAnchorEdits.set(id, { filePath, anchor });
    this.scheduleAnchorEditFlush();
  }

  async flushAnchorEdits({ allowDuringReset = false } = {}) {
    if (this.highlightResetting && !allowDuringReset) return;
    if (this.anchorEditFlushTimer) clearTimeout(this.anchorEditFlushTimer);
    this.anchorEditFlushTimer = null;
    if (!this.pendingAnchorEdits?.size) return;
    const pending = this.pendingAnchorEdits;
    this.pendingAnchorEdits = new Map();
    const byFile = new Map();
    for (const [id, { filePath, anchor }] of pending) {
      if (!byFile.has(filePath)) byFile.set(filePath, { updates: [], deleted: [] });
      if (anchor) byFile.get(filePath).updates.push(anchor);
      else byFile.get(filePath).deleted.push({ id });
    }
    const previous = this.highlightWriteTail || Promise.resolve();
    const operation = previous.catch(() => {}).then(async () => {
      for (const [filePath, record] of byFile) {
        await this.appendHighlightRecord(filePath, { ...record, at: Date.now() });
      }
    });
    this.highlightWriteTail = operation;
    try { await operation; this.lastAnchorEditError = ""; this.anchorEditRetries = 0; }
    catch (error) {
      for (const [id, item] of pending) {
        if (!this.pendingAnchorEdits.has(id)) this.pendingAnchorEdits.set(id, item);
      }
      throw error;
    }
  }

  anchorsForFile(filePath) {
    const store = this.highlightStore;
    if (!store || !store.files) return [];
    return store.files[filePath] || [];
  }

  armPaintProbe(id) {
    if (!this.pendingPaintProbes) this.pendingPaintProbes = new Map();
    const probe = { id, render_registered_wall_ms: null,
      visual_frame_wall_ms: null, visual_confirmed: false, surface: "unknown" };
    probe.promise = new Promise((resolve) => { probe.resolve = resolve; });
    this.pendingPaintProbes.set(id, probe);
    return probe;
  }

  signalPaintProbe(id, surface, isVisible) {
    const probe = this.pendingPaintProbes?.get(id);
    if (!probe || probe.render_registered_wall_ms) return;
    probe.render_registered_wall_ms = Date.now();
    probe.surface = surface;
    const frame = () => {
      probe.visual_frame_wall_ms = Date.now();
      try { probe.visual_confirmed = !!isVisible(); } catch (_error) { probe.visual_confirmed = false; }
      probe.resolve();
    };
    if (typeof requestAnimationFrame === "function") requestAnimationFrame(frame);
    else setTimeout(frame, 0);
  }

  async finishPaintProbe(probe) {
    if (!probe) return null;
    let timer;
    try {
      await Promise.race([probe.promise, new Promise((resolve) => {
        timer = setTimeout(resolve, 450);
      })]);
      return { render_registered_wall_ms: probe.render_registered_wall_ms,
        visual_frame_wall_ms: probe.visual_frame_wall_ms,
        visual_confirmed: probe.visual_confirmed, surface: probe.surface };
    } finally {
      clearTimeout(timer);
      this.pendingPaintProbes?.delete(probe.id);
    }
  }

  async addAnchor(filePath, anchor, onSaved = null, traceSteps = null) {
    if (this.highlightResetting) throw new Error("正在清空高光，请稍后重新划选。");
    const store = await this.loadHighlights();
    if (this.highlightResetting) throw new Error("正在清空高光，请稍后重新划选。");
    const previous = this.highlightWriteTail || Promise.resolve();
    const operation = previous.catch(() => {}).then(async () => {
      const started = Date.now();
      await this.appendAnchorRecord(filePath, anchor);
      this.lastHighlightWriteMs = Date.now() - started;
      const commitWallMs = Date.now();
      this.lastHighlightWriteWallMs = commitWallMs;
      if (traceSteps) traceSteps.storage_commit_wall_ms = commitWallMs;
      // 在同一个写入回调里注册绘制：不让 await 写队列的下一轮调度
      // 把「盘上已有记录 → 页面显色」白白拉长一个 renderer 时钟周期。
      if (!store.files[filePath]) store.files[filePath] = [];
      store.files[filePath].push(anchor);
      this.highlightById?.set(anchor.id, { filePath, anchor });
      if (traceSteps) traceSteps.memory_updated_wall_ms = Date.now();
      this.updateOpenAnnotation(filePath, anchor);
      this.scheduleHighlightSidebarRefresh?.();
      if (traceSteps) traceSteps.annotation_cache_updated_wall_ms = Date.now();
      // 已登记的颜色快照不需要每次遍历所有锚点、重写整张 stylesheet。
      const needsPalette = !this.highlightPaletteStyle?.isConnected
        || !this.highlightPaletteHexes?.has(anchorColorHex(anchor));
      if (traceSteps) traceSteps.palette_rebuilt = needsPalette;
      if (needsPalette) this.applyHighlightPalette?.();
      if (traceSteps) traceSteps.palette_ready_wall_ms = Date.now();
      try { this.pushEditorAnchor(filePath, anchor, traceSteps); }
      catch (error) { this.lastEditorError = `编辑器绘制：${error.message || error}`; }
      if (traceSteps) traceSteps.editor_path_done_wall_ms = Date.now();
      if (onSaved) {
        if (traceSteps) traceSteps.clear_selection_start_wall_ms = Date.now();
        try { onSaved(); }
        catch (error) { this.lastSelectionError = String(error.message || error); }
        if (traceSteps) traceSteps.clear_selection_end_wall_ms = Date.now();
      }
      // 编辑视图已由 CodeMirror 增量绘制；只有 PDF 或同文件阅读视图
      // 需要收集 Range。不要为一条 MD 高光重算所有打开的 PDF 页面。
      const needsDomRefresh = anchor.kind === "pdf"
        || [...(this.readingSurfaces || [])].some((surface) =>
          surface.filePath === filePath && surface.element.isConnected);
      if (traceSteps) traceSteps.dom_refresh_needed = needsDomRefresh;
      if (needsDomRefresh) {
        if (traceSteps) traceSteps.dom_refresh_start_wall_ms = Date.now();
        try { this.refreshHighlights(); }
        catch (error) {
          this.lastRangeError = `新增高光重画：${error.message || error}`;
          this.scheduleHighlightRefresh();
        }
        if (traceSteps) {
          traceSteps.dom_refresh_end_wall_ms = Date.now();
          traceSteps.dom_refresh_ms = this.lastHighlightRefreshMs ?? null;
        }
      }
      return commitWallMs;
    });
    this.highlightWriteTail = operation;
    return operation;
  }

  findHighlight(id) {
    if (!id || !this.highlightStore?.files) return null;
    if (this.highlightById) return this.highlightById.get(id) || null;
    for (const [filePath, anchors] of Object.entries(this.highlightStore.files)) {
      const anchor = anchors.find((item) => item.id === id);
      if (anchor) return { filePath, anchor };
    }
    return null;
  }

  async changeHighlight(id, patch, remove = false) {
    if (this.highlightResetting) throw new Error("正在清空高光，请稍后操作。");
    // 注释采用按 ID 合并的 patch 事件；编辑器可同时更新 quote，两个事件
    // 无论先后都不会用旧 quote 或旧注释覆盖对方。
    await this.flushAnchorEdits();
    if (this.highlightResetting) throw new Error("正在清空高光，请稍后操作。");
    const found = this.findHighlight(id);
    if (!found) throw new Error("高光已被删除或源文件已移动。");
    const { filePath } = found;
    if (remove) {
      if (!this.deletingHighlights) this.deletingHighlights = new Set();
      this.deletingHighlights.add(id);
    }
    const previous = this.highlightWriteTail || Promise.resolve();
    const operation = previous.catch(() => {}).then(() =>
      this.appendHighlightRecord(filePath, remove
        ? { deleted: [{ id }], at: Date.now() }
        : { patches: [{ id, ...patch }], at: Date.now() }));
    this.highlightWriteTail = operation;
    try { await operation; }
    catch (error) {
      if (remove) this.deletingHighlights.delete(id);
      throw error;
    }
    const current = this.findHighlight(id);
    if (current) {
      const anchors = this.highlightStore.files[filePath];
      const index = anchors.findIndex((item) => item.id === id);
      if (index >= 0) {
        if (remove) anchors.splice(index, 1);
        else {
          anchors[index] = { ...anchors[index], ...patch };
          // 与重放同一套派生规则：只有注释类字段变化才算「修改注释」。这里取内存
          // 更新时刻，与记录里写入的 `at` 相差不超过一次写盘排队，分钟粒度看不出。
          if (touchesAnnotation(patch)) anchors[index].updatedAt = Date.now();
        }
        if (!remove && this.highlightById) this.highlightById.set(id, { filePath, anchor: anchors[index] });
        this.updateOpenAnnotation(filePath, remove ? null : anchors[index], id);
      }
    }
    if (!remove && this.pendingAnchorEdits?.get(id)?.anchor) {
      const pending = this.pendingAnchorEdits.get(id);
      pending.anchor = { ...pending.anchor, ...patch };
      if (touchesAnnotation(patch)) pending.anchor.updatedAt = Date.now();
    }
    if (remove) {
      this.highlightById?.delete(id);
      this.pendingAnchorEdits?.delete(id);
      this.deletingHighlights.delete(id);
      this.refreshEditorDecorations();
    }
    if (!remove && patch.color) this.refreshEditorDecorations();
    if (remove || patch.colorHex) this.applyHighlightPalette();
    this.scheduleHighlightRefresh();
    this.scheduleHighlightSidebarRefresh?.();
    return this.findHighlight(id)?.anchor || null;
  }

  highlightAtPoint(event) {
    const mark = event.target?.closest?.("[data-nh-highlight-id]");
    const directId = mark?.getAttribute("data-nh-highlight-id");
    if (directId && this.findHighlight(directId)) return directId;
    for (const item of this.interactiveRanges || []) {
      if (!item.root.isConnected || !item.root.contains(event.target)) continue;
      if ([...item.range.getClientRects()].some((rect) => event.clientX >= rect.left && event.clientX <= rect.right && event.clientY >= rect.top && event.clientY <= rect.bottom)) return item.id;
    }
    return null;
  }
  positionHighlightPopup(el, event) {
    const width = el.classList.contains("nh-highlight-preview") ? 420 : 290;
    const height = el.classList.contains("nh-highlight-preview") ? 340 : 160;
    el.style.left = `${Math.max(8, Math.min(window.innerWidth - width - 8, event.clientX + 12))}px`;
    el.style.top = `${Math.max(8, Math.min(window.innerHeight - height - 8, event.clientY + 16))}px`;
  }
  onHighlightMove(event) {
    if (this.highlightMenu?.contains(event.target)) {
      this.hideHighlightPreview();
      return;
    }
    if (this.highlightPreview?.contains(event.target)) {
      if (this.hoverExitTimer) clearTimeout(this.hoverExitTimer);
      this.hoverExitTimer = null;
      return;
    }
    if (String(document.getSelection?.() || "").trim()) {
      this.hideHighlightPreview();
      return;
    }
    const id = this.highlightAtPoint(event);
    if (!id && this.highlightPreview) {
      if (!this.hoverExitTimer) this.hoverExitTimer = setTimeout(() => {
        this.hoverExitTimer = null;
        this.hoverHighlightId = null;
        this.hideHighlightPreview();
      }, 180);
      return;
    }
    if (this.hoverExitTimer) clearTimeout(this.hoverExitTimer);
    this.hoverExitTimer = null;
    if (id === this.hoverHighlightId) return;
    this.hoverHighlightId = id;
    this.hideHighlightPreview();
    if (this.hoverTimer) clearTimeout(this.hoverTimer);
    if (!id) return;
    const point = { clientX: event.clientX, clientY: event.clientY };
    this.hoverTimer = setTimeout(() => {
      this.hoverTimer = null;
      if (this.hoverHighlightId !== id || this.highlightMenu) return;
      const anchor = this.annotationForHighlight(id);
      const note = anchor?.note || "";
      // 只设了注释标题也算有内容：悬停要能看到它，否则标题只能去侧栏里找。
      if (!note && !anchor?.images?.length && !anchor?.title) return;
      const preview = document.createElement("div");
      preview.className = "nh-highlight-preview";
      this.renderAnnotationBody(preview, anchor);
      // 悬停预览没有划线原文，标题就排在最上面（侧栏里它排在原文下面）。放在
      // renderAnnotationBody 之后 prepend：那个方法一上来就 replaceChildren。
      const title = this.buildAnnotationTitle(anchor);
      if (title) preview.prepend(title);
      this.positionHighlightPopup(preview, point);
      document.body.appendChild(preview);
      this.highlightPreview = preview;
    }, 120);
  }
  hideHighlightPreview() {
    if (this.hoverTimer) clearTimeout(this.hoverTimer);
    if (this.hoverExitTimer) clearTimeout(this.hoverExitTimer);
    this.hoverTimer = null;
    this.hoverExitTimer = null;
    this.highlightPreview?.remove();
    this.highlightPreview = null;
  }
  closeHighlightMenu() {
    this.highlightMenu?.remove();
    this.highlightMenu = null;
  }
  onHighlightClick(event) {
    if (this.highlightMenu?.contains(event.target)) return;
    if (this.highlightMenu) this.closeHighlightMenu();
    if (!isPlainLeftClick(event) || event.defaultPrevented || event.target?.closest?.(".modal, a, button, input, textarea") || String(document.getSelection?.() || "").trim()) return;
    const id = this.highlightAtPoint(event);
    if (id && this.findHighlight(id)) this.openHighlightMenu(id, event);
  }
  openHighlightMenu(id, event) {
    this.hideHighlightPreview();
    this.closeHighlightMenu();
    const menu = document.createElement("div");
    menu.className = "nh-highlight-menu";
    menu.setAttribute("role", "menu");
    this.positionHighlightPopup(menu, event);
    const action = (label, handler, host = menu) => {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = label;
      button.addEventListener("click", (click) => {
        click.stopPropagation();
        Promise.resolve().then(handler).catch((error) => new Notice(`\u9AD8\u5149\u64CD\u4F5C\u5931\u8D25\uFF1A${error.message || error}`));
      });
      host.appendChild(button);
      return button;
    };
    action("\u7F16\u8F91\u6CE8\u91CA", () => {
      this.closeHighlightMenu();
      this.openHighlightActions(id);
    });
    // \u8FD9\u91CC\u539F\u672C\u6709\u300C\u8DF3\u8F6C\u4F4D\u7F6E\u300D\u3002\u5B83\u590D\u7528\u4FA7\u680F \u2197 \u7684 openHighlightById\uFF08\u6253\u5F00\u6765\u6E90\u6587\u6863\u3001PDF \u843D\u5230
    // \u9875\u7801\u3001Markdown \u9009\u4E2D\u539F\u6587\u8303\u56F4\uFF09\uFF0C\u4F46\u6253\u5F00\u8FD9\u4EFD\u83DC\u5355\u7684\u524D\u63D0\u5C31\u662F\u300C\u9F20\u6807\u6B63\u70B9\u5728\u4E00\u6761\u9AD8\u5149\u4E0A\u300D\u2014\u2014
    // \u4EBA\u5728\u9AD8\u5149\u5904\uFF0C\u8DF3\u8FC7\u53BB\u7B49\u4E8E\u539F\u5730\u4E0D\u52A8\uFF0C\u6240\u4EE5\u5220\u6389\u3002\u4FA7\u680F\u90A3\u6761 \u2197 \u7559\u7740\uFF1A\u5217\u8868\u662F\u8DE8\u6587\u6863\u6C47\u603B\u7684\uFF0C
    // \u90A3\u91CC\u624D\u771F\u7684\u9700\u8981\u300C\u53BB\u539F\u6587\u770B\u4E00\u773C\u300D\u3002
    // \u4E0B\u9762\u8FD9\u6761\u300C\u8DF3\u8F6C\u4FA7\u680F\u300D\u4E0D\u662F\u5B83\u56DE\u6765\u4E86\uFF1A\u53BB\u7684\u662F\u8DE8\u6587\u6863\u6C47\u603B\u7684\u4FA7\u680F\u5361\u7247\uFF08\u53EF\u80FD\u8FDE\u6587\u6863\u90FD\u4E0D\u540C\uFF09\uFF0C
    // \u800C\u4E0D\u662F\u6B64\u523B\u9F20\u6807\u4E0B\u65B9\u7684\u6E90\u6587\u4F4D\u7F6E\uFF0C\u6240\u4EE5\u4E0D\u5B58\u5728\u300C\u539F\u5730\u4E0D\u52A8\u300D\u7684\u95EE\u9898\u3002
    action("\u8DF3\u8F6C\u4FA7\u680F", async () => {
      this.closeHighlightMenu();
      await this.revealHighlightInSidebar(id);
    });
    action("\u590D\u5236\u94FE\u63A5", async () => {
      await this.copyHighlightLink(id);
      this.closeHighlightMenu();
    });
    action("\u5220\u9664\u9AD8\u5149", async () => {
      if (!window.confirm("\u5220\u9664\u8FD9\u6761\u9AD8\u5149\u53CA\u6CE8\u91CA\uFF1F\u6E90\u6587\u4EF6\u4E0D\u4F1A\u6539\u53D8\u3002")) return;
      await this.changeHighlight(id, {}, true);
      this.closeHighlightMenu();
      new Notice("\u9AD8\u5149\u5DF2\u5220\u9664");
    });
    const colorRow = document.createElement("div");
    colorRow.className = "nh-highlight-color-row";
    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.textContent = "\u5207\u6362\u989C\u8272 \u25BE";
    colorRow.appendChild(toggle);
    const swatches = document.createElement("div");
    swatches.className = "nh-highlight-swatches";
    // 只列配置的那 3 种，和划选悬浮窗保持一致：菜单里要是能改成配置外的颜色，
    // 悬浮窗就再也改不回来了。
    for (const color of this.activeHighlightColors()) {
      const button = action("", async () => {
        await this.changeHighlight(id, { color,
          colorHex: DEFAULT_HIGHLIGHT_PALETTE[color] });
        this.refreshEditorDecorations();
        this.closeHighlightMenu();
      }, swatches);
      button.classList.add(`nh-hl-${color}`);
      button.setAttribute("aria-label", `切换到${HIGHLIGHT_COLOR_DEFS[color].label}`);
    }
    colorRow.appendChild(swatches);
    toggle.addEventListener("click", () => colorRow.classList.toggle("is-open"));
    menu.appendChild(colorRow);
    document.body.appendChild(menu);
    this.highlightMenu = menu;
  }
  async copyHighlightLink(id) {
    const link = `obsidian://noting-helper-highlight?vault=${encodeURIComponent(this.app.vault.getName())}&id=${encodeURIComponent(id)}`;
    await navigator.clipboard.writeText(link);
    new Notice("\u5DF2\u590D\u5236\u9AD8\u5149\u5B9A\u4F4D\u94FE\u63A5");
  }
  existingHighlightTags() {
    const found = /* @__PURE__ */ new Set();
    for (const file of this.app.vault.getMarkdownFiles?.() || []) {
      const cache = this.app.metadataCache?.getFileCache?.(file);
      for (const tag of cache?.tags || []) if (tag.tag) found.add(tag.tag);
    }
    return [...found].sort((a, b) => a.localeCompare(b));
  }
  openHighlightActions(id) {
    const found = this.findHighlight(id);
    if (!found) return;
    const modal = new Modal(this.app);
    const body = modal.contentEl;
    body.replaceChildren();
    const heading = body.ownerDocument.createElement("h3");
    heading.textContent = "\u7F16\u8F91\u9AD8\u5149";
    body.appendChild(heading);
    const quote = body.ownerDocument.createElement("p");
    quote.textContent = found.anchor.exact || "";
    body.appendChild(quote);
    // 弹窗不传 onCancel：关掉弹窗本来就有多种方式（点外部、Esc），按钮区维持只有
    // 「保存」的原样。侧栏那个就地展开的编辑器才需要显式的「取消」来收起。
    const built = this.buildAnnotationEditor({
      id,
      anchor: found.anchor,
      doc: body.ownerDocument,
      onSaved: () => modal.close(),
    });
    body.appendChild(built.element);
    modal.onClose = built.dispose;
    modal.open();
  }
  // \u6CE8\u91CA\u7F16\u8F91\u5668\u7684\u552F\u4E00\u5B9E\u73B0\u3002\u5F39\u7A97\u5165\u53E3\u4E0E\u4FA7\u680F\u7684\u5C31\u5730\u5C55\u5F00\u5171\u7528\u5B83\uFF0C\u56FE\u7247\u843D\u76D8\u3001\u6807\u7B7E\u6821\u9A8C\u4E0E
  // \u5931\u8D25\u56DE\u6EDA\u56E0\u6B64\u53EA\u6709\u4E00\u4EFD\uFF0C\u4E0D\u4F1A\u968F\u65F6\u95F4\u5206\u53C9\u3002
  //
  // \u7ED3\u6784\u6309\u8F6F\u4EF6\u81EA\u5DF1\u7684\u6CE8\u91CA\u6A21\u578B\u6765\uFF1A\u6CE8\u91CA\u6B63\u6587\uFF08textarea\uFF0C\u542B `[\u56FE\u7247:name]` \u884C\u6807\u8BB0\uFF09\u2192
  // \u5DF2\u6DFB\u52A0\u56FE\u7247\u680F \u2192 \u6807\u7B7E\u8F93\u5165\uFF08chips / \u5DF2\u6709\u6807\u7B7E\u6C60 / \u8F93\u5165\u8054\u60F3\uFF09\u3002\u4FA7\u680F\u8981\u6C42\u56FE\u7247\u680F\u53EA\u5728
  // \u5C55\u5F00\u540E\u624D\u51FA\u73B0\uFF0C\u56E0\u6B64\u56FE\u7247\u680F\u5C5E\u4E8E\u7F16\u8F91\u5668\u5185\u90E8\uFF0C\u6298\u53E0\u6001\u770B\u4E0D\u5230\u5B83\u3002
  // actionsEl：调用方指定按钮行时，把「保存/取消」挂进去，而不是在编辑器末尾另起一行。
  // 侧栏就地展开传的是卡片上 ✎/↗/🗑 那一行——按钮就和操作图标同处一行、紧贴编辑框上方，
  // 不用滚到底部去找；弹窗不传，维持自己那一行只有「保存」的原样。
  buildAnnotationEditor({ id, anchor, doc, onSaved, onCancel, actionsEl }) {
    const container = doc.createElement("div");
    container.className = "nh-highlight-annotation-editor";
    const body = container;
    const note = body.ownerDocument.createElement("textarea");
    note.placeholder = "\u6DFB\u52A0\u6216\u7F16\u8F91\u6CE8\u91CA\uFF1B\u53EF Ctrl+V \u7C98\u8D34\u56FE\u7247";
    note.value = this.annotationLayout(anchor);
    note.className = "nh-highlight-note";
    // 编辑框高度跟着内容走：textarea 默认是固定高度，注释一长就变成框内的小滚动条，
    // 侧栏就地展开时尤其憋屈。这里按 scrollHeight 现算高度、上限 20 行正文，再多才回落到
    // 框内滚动——所以「编辑时要向下长」和「不许盖满整窗」两条同时成立。
    // 上限按行数算而不是按屏幕比例：原来取半屏，实测宽屏上还是太高。行高从 textarea 自己
    // 的计算样式读（line-height 为 normal 或测试环境读不到就按字号 ×1.4 估）；窗口本身比
    // 20 行还矮时再压回半屏以内，免得编辑器比可视区还高。弹窗与侧栏就地展开共用这一段，
    // 所以两边的上限都是 20 行。
    // 拿不到布局（元素还没挂上、或测试环境 scrollHeight 恒为 0）就不写内联高度，
    // 交给 CSS 的 min-height，绝不把高度设成 0。
    const EDITOR_MAX_LINES = 20;
    const autoGrow = () => {
      if (!note.isConnected || !note.scrollHeight) return;
      const view = note.ownerDocument.defaultView;
      const style = view?.getComputedStyle?.(note);
      const fontSize = Number.parseFloat(style?.fontSize || "") || 16;
      const lineHeight = Number.parseFloat(style?.lineHeight || "") || fontSize * 1.4;
      const cap = Math.min(
        Math.round(lineHeight * EDITOR_MAX_LINES),
        Math.max(120, Math.round((view?.innerHeight || 800) * 0.5)),
      );
      note.style.height = "auto";
      // scrollHeight 不含边框，border-box 下要把边框补回去，否则右边永远差一条滚动条。
      const needed = note.scrollHeight + (note.offsetHeight - note.clientHeight);
      note.style.height = `${Math.min(needed, cap)}px`;
      note.style.overflowY = needed > cap ? "auto" : "hidden";
    };
    note.addEventListener("input", autoGrow);
    // textarea 里 Tab 的默认行为是把焦点交给下一个控件（跳到标签框或「保存」），注释
    // 正文因此没法缩进。这里就地接管：无选区插一个缩进，有选区整块缩进；Shift+Tab
    // 退一级。stopPropagation 是为了不再触发 Obsidian 自己的 Tab 快捷键。
    note.addEventListener("keydown", (event) => {
      if (event.key !== "Tab" || event.ctrlKey || event.metaKey || event.altKey || event.isComposing) return;
      event.preventDefault();
      event.stopPropagation();
      const indent = "\t";
      const start = note.selectionStart ?? 0;
      const end = note.selectionEnd ?? start;
      const value = note.value;
      const lineStart = value.lastIndexOf("\n", start - 1) + 1;
      const breakAt = value.indexOf("\n", end);
      const lineEnd = breakAt < 0 ? value.length : breakAt;
      if (start === end) {
        if (!event.shiftKey) {
          note.setRangeText(indent, start, end, "end");
        } else {
          // 只吃掉光标前紧挨着的那个缩进单位，与上面插入的制表符一一对应。
          const before = value.slice(lineStart, start);
          if (before.endsWith(indent)) note.setRangeText("", start - indent.length, start, "end");
        }
      } else {
        const lines = value.slice(lineStart, lineEnd).split("\n");
        const next = lines.map((line) => {
          if (event.shiftKey) return line.startsWith(indent) ? line.slice(indent.length) : line;
          return line ? indent + line : line;   // 空行不加缩进，免得留下尾随空白
        }).join("\n");
        note.setRangeText(next, lineStart, lineEnd, "select");
      }
      autoGrow();
    });
    // 首次挂到卡片/弹窗上之后再量一次：脱开文档时 scrollHeight 为 0，量不出真实高度。
    const growFrame = note.ownerDocument.defaultView?.requestAnimationFrame?.(() => autoGrow());
    // 注释标题：正文编辑框上面单独一条窄框。它同时决定导出的注释目录名
    // （annotationFolderName），占位文案里把这件事说明白。
    const titleInput = body.ownerDocument.createElement("input");
    titleInput.type = "text";
    titleInput.className = "nh-annotation-title-input";
    titleInput.placeholder = "注释标题（可选，导出时用作注释目录名）";
    titleInput.value = anchor.title || "";
    titleInput.setAttribute("aria-label", "注释标题");
    body.appendChild(titleInput);
    const editor = body.ownerDocument.createElement("div");
    editor.className = "nh-highlight-editor";
    const codeTools = doc.createElement("div");
    codeTools.className = "nh-highlight-code-tools";
    const codeMenu = doc.createElement("details");
    codeMenu.className = "nh-highlight-code-menu";
    const codeButton = doc.createElement("summary");
    codeButton.textContent = "代码块";
    codeButton.title = "先选中文字，再选择代码语言";
    // Default mouse focus would hide the textarea's selected-text highlight.
    // Keep the native selection visible throughout menu browsing; click still
    // toggles <details>, and conversion collapses the selection afterwards.
    const keepNoteSelection = (event) => {
      if (event.button === 0 && note.ownerDocument.activeElement === note) event.preventDefault();
    };
    codeButton.addEventListener("mousedown", keepNoteSelection);
    codeMenu.appendChild(codeButton);
    const codeChoices = doc.createElement("div");
    codeChoices.className = "nh-highlight-code-choices";
    codeMenu.appendChild(codeChoices);
    const codeStatus = doc.createElement("span");
    codeStatus.setAttribute("role", "status");
    const insertCode = (requestedLanguage) => {
      codeMenu.open = false;
      const start = note.selectionStart ?? 0;
      const end = note.selectionEnd ?? start;
      const selected = note.value.slice(start, end);
      if (start === end) {
        codeStatus.textContent = "请先用鼠标选择要转换为代码块的文字。";
        return;
      }
      if (!selected.trim()) {
        codeStatus.textContent = "代码内容不能为空。";
        return;
      }
      let language = requestedLanguage;
      if (language === null) {
        language = "";
        try {
          const parsed = JSON.parse(selected);
          if (parsed && typeof parsed === "object") language = "json";
        } catch (_) { /* Ordinary code is not JSON. */ }
        if (!language) {
          // Keep the same conservative checks and order as recommend_code_language.
          const checks = [
            ["python", /^\s*(?:def|class)\s+\w+.*:\s*$|^\s*(?:from\s+[\w.]+\s+import|import\s+[\w.]+)|if\s+__name__\s*==/m],
            ["java", /^\s*(?:public\s+)?class\s+\w+|System\.out\.|^\s*(?:package|import\s+java\.)\s+/m],
            ["typescript", /^\s*(?:interface|type|enum)\s+\w+|:\s*(?:string|number|boolean)(?:\[\])?\b/m],
            ["javascript", /^\s*(?:const|let|var)\s+\w+\s*=.*(?:=>|require\()|\bconsole\.log\(|^\s*(?:async\s+)?function\s+\w+/m],
            ["cpp", /^\s*#include\s*<(?:iostream|vector|string|map|memory|algorithm)>|\bstd::|\bcout\s*<<|\bcin\s*>>/m],
            ["csharp", /^\s*using\s+System(?:\.|;)|\bConsole\.WriteLine\(|\bnamespace\s+\w+/m],
            ["sql", /\bSELECT\b.+\bFROM\b|\bINSERT\s+INTO\b|\bUPDATE\b.+\bSET\b|\bCREATE\s+TABLE\b/is],
            ["bash", /^\s*#!.*\b(?:ba|z|k)?sh\b|^\s*(?:export\s+\w+=|(?:if|for|while)\s+.+;\s*then\b)/m],
            ["html", /<!DOCTYPE\s+html|<html\b|<(?:div|section|main|article|script|style)\b[^>]*>/is],
            ["css", /^\s*(?:[.#][\w-]+|[a-z][\w-]*)[^\n{]*\{\s*$[\s\S]*^\s*[\w-]+\s*:\s*[^;]+;/m],
            ["c", /^\s*#include\s*[<"](?:stdio|stdlib|string|stdint)\.h[>"]|\bprintf\s*\(|\bscanf\s*\(/m],
          ];
          language = checks.find(([, pattern]) => pattern.test(selected))?.[0] || "";
        }
      }
      // Longer fences also safely contain Markdown examples with backticks.
      const fence = "`".repeat(Math.max(3, ...Array.from(selected.matchAll(/`+/g), m => m[0].length + 1)));
      const before = note.value.slice(0, start);
      const after = note.value.slice(end);
      const prefix = !before || before.endsWith("\n\n") ? "" : before.endsWith("\n") ? "\n" : "\n\n";
      const suffix = !after || after.startsWith("\n\n") ? "" : after.startsWith("\n") ? "\n" : "\n\n";
      const block = `${prefix}${fence}${language}\n${selected}${selected.endsWith("\n") ? "" : "\n"}${fence}${suffix}`;
      note.setRangeText(block, start, end, "end");
      note.focus();
      note.dispatchEvent(new doc.defaultView.Event("input", { bubbles: true }));
      codeStatus.textContent = `已作为 ${language || "纯文本"} 代码块插入。`;
    };
    for (const [label, language] of [
      ["自动推荐", null], ["纯文本", ""], ["Python", "python"],
      ["Java", "java"], ["JavaScript", "javascript"], ["TypeScript", "typescript"],
      ["C", "c"], ["C++", "cpp"], ["C#", "csharp"], ["SQL", "sql"],
      ["Shell", "bash"], ["JSON", "json"], ["HTML", "html"], ["CSS", "css"],
    ]) {
      const choice = doc.createElement("button");
      choice.type = "button";
      choice.textContent = label;
      choice.addEventListener("mousedown", keepNoteSelection);
      choice.addEventListener("click", () => insertCode(language));
      codeChoices.appendChild(choice);
    }
    codeTools.append(codeMenu, codeStatus);
    // Keep the dropdown outside the image editor's clipped rounded border.
    body.appendChild(codeTools);
    note.addEventListener("focus", () => { codeMenu.open = false; });
    editor.appendChild(note);
    const imageGallery = body.ownerDocument.createElement("div");
    imageGallery.className = "nh-highlight-editor-images";
    editor.appendChild(imageGallery);
    body.appendChild(editor);
    const imageEntries = [];
    const imageUrls = /* @__PURE__ */ new Map();
    // 签名（图片名按正文行序拼起来）本身可能是空串，所以「还没渲染过」要用 null 表示。
    // 原来初始值是空串：删掉最后一张图片时剩下的可见集合恰好也是空串，会被当成
    // 「没变化」而直接返回，图片栏就留着那张已经解除关联的缩略图，直到重开编辑器。
    let gallerySignature = null;
    let activeLightbox = null;
    const renderImages = () => {
      const lines = note.value.split("\n").map((line) => line.trim());
      const visible = imageEntries.map((entry) => ({ entry, index: lines.indexOf(`[\u56FE\u7247:${entry.name}]`) })).filter((item) => item.index >= 0).sort((a, b) => a.index - b.index).map((item) => item.entry);
      const signature = visible.map((entry) => entry.name).join("\0");
      if (signature === gallerySignature) return;
      gallerySignature = signature;
      imageGallery.replaceChildren();
      for (const entry of visible) {
        const card = body.ownerDocument.createElement("figure");
        const img = body.ownerDocument.createElement("img");
        img.alt = entry.name;
        img.loading = "lazy";
        if (entry.path) {
          img.src = this.annotationImageResource(entry.path);
        } else if (entry.file) {
          try {
            const url = imageUrls.get(entry.file) || URL.createObjectURL(entry.file);
            imageUrls.set(entry.file, url);
            img.src = url;
          } catch (_error) {
            try {
              const reader = new body.ownerDocument.defaultView.FileReader();
              reader.onload = () => {
                if (typeof reader.result === "string") img.src = reader.result;
              };
              reader.readAsDataURL(entry.file);
            } catch (_unsupported) {
            }
          }
        }
        img.title = `\u70B9\u51FB\u9884\u89C8 ${entry.name}`;
        img.addEventListener("click", () => {
          if (!img.src) return;
          const lightbox = body.ownerDocument.createElement("div");
          lightbox.className = "nh-highlight-lightbox";
          lightbox.setAttribute("role", "dialog");
          lightbox.setAttribute("aria-label", `\u9884\u89C8 ${entry.name}`);
          const full = body.ownerDocument.createElement("img");
          full.src = img.src;
          full.alt = entry.name;
          lightbox.appendChild(full);
          lightbox.addEventListener("click", () => lightbox.remove());
          body.ownerDocument.body.appendChild(lightbox);
          activeLightbox = lightbox;
        });
        const remove = body.ownerDocument.createElement("button");
        remove.type = "button";
        remove.className = "nh-highlight-image-remove";
        remove.textContent = "\xD7";
        remove.title = `\u79FB\u9664 ${entry.name}`;
        remove.setAttribute("aria-label", `\u79FB\u9664 ${entry.name}`);
        remove.addEventListener("click", () => {
          const marker = `[\u56FE\u7247:${entry.name}]`;
          const lines2 = note.value.split("\n");
          const index = lines2.findIndex((line) => line.trim() === marker);
          if (index >= 0) {
            lines2.splice(index, 1);
            note.value = lines2.join("\n");
          }
          const at = imageEntries.indexOf(entry);
          if (at >= 0) imageEntries.splice(at, 1);
          gallerySignature = null;
          renderImages();
          autoGrow();
          note.focus();
        });
        card.appendChild(remove);
        card.appendChild(img);
        const caption = body.ownerDocument.createElement("figcaption");
        caption.textContent = entry.name;
        card.appendChild(caption);
        imageGallery.appendChild(card);
      }
    };
    note.addEventListener("input", renderImages);
    // 挂进卡片按钮行的「保存/取消」不在编辑器子树里，收起时得自己摘掉。
    const mountedActions = [];
    // 编辑器被收起/关闭时的清理：临时图片 URL 必须回收，灯箱不能留在页面上。
    const dispose = () => {
      if (growFrame !== void 0) note.ownerDocument.defaultView?.cancelAnimationFrame?.(growFrame);
      activeLightbox?.remove();
      for (const url of imageUrls.values()) URL.revokeObjectURL(url);
      imageUrls.clear();
      for (const el of mountedActions) el.remove();
    };
    const usedNames = /* @__PURE__ */ new Set();
    const uniqueName = (raw) => {
      const base = String(raw || "\u526A\u8D34\u677F\u56FE\u7247.png").replace(/[\r\n\[\]]/g, "_").trim() || "\u526A\u8D34\u677F\u56FE\u7247.png";
      let name = base;
      for (let index = 2; usedNames.has(name); index += 1) name = `${base} (${index})`;
      usedNames.add(name);
      return name;
    };
    const insertImage = (entry, atCaret = false) => {
      entry.name = uniqueName(entry.name);
      imageEntries.push(entry);
      const marker = `[\u56FE\u7247:${entry.name}]`;
      const cursor = atCaret ? note.selectionStart ?? note.value.length : note.value.length;
      const before = note.value.slice(0, cursor);
      const after = note.value.slice(cursor);
      const insertion = `${before && !before.endsWith("\n") ? "\n" : ""}${marker}${after && !after.startsWith("\n") ? "\n" : ""}`;
      note.setRangeText(insertion, cursor, cursor, "end");
      renderImages();
      // setRangeText 不派发 input，长度变化（含加图造成的换行）要自己量一次。
      autoGrow();
    };
    (anchor.images || []).forEach((image, index) => {
      const name = anchor.imageNames?.[index] || path.posix.basename(image);
      imageEntries.push({ path: image, name });
      usedNames.add(name);
    });
    renderImages();
    const addPendingImage = (file, atCaret = false) => {
      if (!file || !file.type?.startsWith("image/")) return;
      insertImage({ file, name: file.name || "\u526A\u8D34\u677F\u56FE\u7247.png" }, atCaret);
    };
    note.addEventListener("paste", (event) => {
      const items = [...event.clipboardData?.items || []];
      const files = items.length ? items.filter((item) => item.kind === "file" && item.type?.startsWith("image/")).map((item) => item.getAsFile()).filter(Boolean) : [...event.clipboardData?.files || []].filter((file) => file.type?.startsWith("image/"));
      if (!files.length) return;
      event.preventDefault();
      event.stopPropagation();
      for (const file of files) addPendingImage(file, true);
    });
    const imageInput = body.ownerDocument.createElement("input");
    imageInput.type = "file";
    imageInput.accept = "image/png,image/jpeg,image/gif,image/webp,image/bmp";
    imageInput.multiple = true;
    imageInput.hidden = true;
    imageInput.addEventListener("change", () => {
      for (const file of imageInput.files || []) addPendingImage(file);
      imageInput.value = "";
    });
    body.appendChild(imageInput);
    const imageButton = body.ownerDocument.createElement("button");
    imageButton.type = "button";
    imageButton.textContent = "\u6DFB\u52A0\u6CE8\u91CA\u56FE\u7247\u2026";
    imageButton.addEventListener("click", () => imageInput.click());
    body.appendChild(imageButton);
    const tags = body.ownerDocument.createElement("input");
    tags.placeholder = "\u6807\u7B7E\uFF1A\u8F93\u5165 # \u6216\u4ECE\u5DF2\u6709\u6807\u7B7E\u9009\u62E9\uFF1B\u591A\u4E2A\u7528\u7A7A\u683C\u5206\u9694";
    tags.value = (anchor.tags || []).join(" ");
    tags.className = "nh-highlight-tags";
    body.appendChild(tags);
    const tagChips = body.ownerDocument.createElement("div");
    tagChips.className = "nh-highlight-tag-chips";
    body.appendChild(tagChips);
    const renderTagChips = () => {
      tagChips.replaceChildren();
      for (const tag of tags.value.match(/#[\p{L}\p{N}_\-/]+/gu) || []) {
        const chip = body.ownerDocument.createElement("span");
        chip.textContent = tag;
        tagChips.appendChild(chip);
      }
    };
    renderTagChips();
    const pool = body.ownerDocument.createElement("div");
    pool.className = "nh-highlight-tag-pool";
    const select = body.ownerDocument.createElement("select");
    const empty = body.ownerDocument.createElement("option");
    empty.textContent = "\u9009\u62E9\u5DF2\u6709\u6807\u7B7E";
    empty.value = "";
    select.appendChild(empty);
    for (const tag of this.existingHighlightTags()) {
      const option = body.ownerDocument.createElement("option");
      option.value = tag;
      option.textContent = tag;
      select.appendChild(option);
    }
    pool.appendChild(select);
    const addTag = body.ownerDocument.createElement("button");
    addTag.type = "button";
    addTag.textContent = "+";
    addTag.title = "\u9009\u62E9\u540E\u518D\u70B9 +\uFF0C\u63D2\u5165\u5230\u6807\u7B7E\u8F93\u5165\u6846\u7684\u5149\u6807\u5904";
    addTag.addEventListener("click", () => {
      if (!select.value) return;
      const caret = tags.selectionStart ?? tags.value.length;
      const prefix = tags.value.slice(0, caret);
      const separator = prefix && !/\s$/.test(prefix) ? " " : "";
      const inserted = `${separator}${select.value} `;
      tags.value = prefix + inserted + tags.value.slice(caret);
      tags.focus();
      tags.setSelectionRange(caret + inserted.length, caret + inserted.length);
      select.value = "";
      renderTagChips();
    });
    pool.appendChild(addTag);
    body.appendChild(pool);
    const suggestions = body.ownerDocument.createElement("div");
    suggestions.className = "nh-highlight-suggestions";
    body.appendChild(suggestions);
    const availableTags = this.existingHighlightTags();
    let activeSuggestion = 0;
    let matches = [];
    const showTags = () => {
      const before = tags.value.slice(0, tags.selectionStart ?? tags.value.length);
      const token = before.match(/(?:^|\s)(#[\p{L}\p{N}_\-/]*)$/u)?.[1];
      matches = token === void 0 ? [] : availableTags.filter((item) => item.toLocaleLowerCase().startsWith(token.toLocaleLowerCase())).slice(0, 12);
      activeSuggestion = Math.min(activeSuggestion, Math.max(0, matches.length - 1));
      suggestions.replaceChildren();
      renderTagChips();
      for (const [index, match] of matches.entries()) {
        const item = body.ownerDocument.createElement("button");
        item.type = "button";
        item.textContent = match;
        item.className = index === activeSuggestion ? "is-selected" : "";
        item.addEventListener("click", () => chooseTag(index));
        suggestions.appendChild(item);
      }
    };
    const chooseTag = (index) => {
      const selected = matches[index];
      if (!selected) return;
      const end = tags.selectionStart ?? tags.value.length;
      const before = tags.value.slice(0, end);
      const match = before.match(/(?:^|\s)#[\p{L}\p{N}_\-/]*$/u);
      if (!match) return;
      const start = end - match[0].length + (/^\s/.test(match[0]) ? 1 : 0);
      tags.value = `${tags.value.slice(0, start)}${selected} ${tags.value.slice(end)}`;
      renderTagChips();
      tags.focus();
      tags.setSelectionRange(start + selected.length + 1, start + selected.length + 1);
      matches = [];
      suggestions.replaceChildren();
    };
    tags.addEventListener("input", () => {
      activeSuggestion = 0;
      showTags();
    });
    tags.addEventListener("keydown", (event) => {
      if (!matches.length) return;
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        activeSuggestion = (activeSuggestion + (event.key === "ArrowDown" ? 1 : -1) + matches.length) % matches.length;
        showTags();
      } else if (event.key === "Enter" || event.key === "Tab") {
        event.preventDefault();
        chooseTag(activeSuggestion);
      } else if (event.key === "Escape") {
        matches = [];
        suggestions.replaceChildren();
      }
    });
    const buttons = actionsEl || body.ownerDocument.createElement("div");
    if (!actionsEl) {
      buttons.className = "nh-highlight-actions";
      body.appendChild(buttons);
    }
    const button = (label, handler, primary) => {
      const el = body.ownerDocument.createElement("button");
      el.textContent = label;
      // \u6302\u8FDB\u5361\u7247\u6309\u94AE\u884C\u7684\u90A3\u4E24\u4E2A\u8D70 .nh-sidebar-edit-action\uFF1A\u5E95\u8272\u53D6\u8FD9\u5F20\u5361\u81EA\u5DF1\u7684\u9AD8\u5149\u8272
      // \uFF08--nh-card-accent \u7531 buildCard \u5185\u8054\uFF09\uFF0C\u300C\u4FDD\u5B58\u300D\u5B9E\u8272\u3001\u662F\u8FD9\u4E00\u884C\u91CC\u552F\u4E00\u7684\u4E3B\u6309\u94AE\u3002
      if (actionsEl) {
        el.className = `nh-sidebar-edit-action${primary ? " is-primary" : ""}`;
        mountedActions.push(el);
      }
      el.addEventListener("click", () => Promise.resolve(handler()).catch((error) => new Notice(`\u9AD8\u5149\u64CD\u4F5C\u5931\u8D25\uFF1A${error.message || error}`)));
      buttons.appendChild(el);
    };
    button("\u4FDD\u5B58", async () => {
      const tokens = tags.value.trim().split(/\s+/).filter(Boolean);
      if (tokens.some((value) => !/^#[\p{L}\p{N}_\-/]+$/u.test(value) || !/[\p{L}_]/u.test(value))) throw new Error("\u6807\u7B7E\u8BF7\u4EE5 # \u5F00\u5934\uFF0C\u4F7F\u7528\u5B57\u6BCD\u3001\u6570\u5B57\u3001\u4E0B\u5212\u7EBF\u3001\u6A2A\u7EBF\u6216\u659C\u7EBF\u3002");
      const byMarker = new Map(imageEntries.map((entry) => [`[\u56FE\u7247:${entry.name}]`, entry]));
      const kept = [];
      const textLines = [];
      for (const line of note.value.split("\n")) {
        const entry = byMarker.get(line.trim());
        if (entry && !kept.includes(entry)) kept.push(entry);
        else textLines.push(line);
      }
      const stored = [];
      try {
        const imagePaths = [];
        for (const entry of kept) {
          if (entry.file) {
            const image = await this.storeHighlightImage(entry.file);
            stored.push(image);
            imagePaths.push(image);
          } else imagePaths.push(entry.path);
        }
        await this.changeHighlight(id, { note: textLines.join("\n").trimEnd(), noteLayout: note.value, tags: [...new Set(tokens)], images: imagePaths, imageNames: kept.map((entry) => entry.name), title: titleInput.value.trim() });
      } catch (error) {
        for (const image of stored) await this.removeUncommittedHighlightImage(image);
        throw error;
      }
      onSaved();
      new Notice("\u9AD8\u5149\u6CE8\u91CA\u5DF2\u4FDD\u5B58");
    }, true);
    if (onCancel) button("\u53D6\u6D88", () => onCancel());
    return { element: container, dispose };
  }
  async openHighlightById(id) {
    const found = this.findHighlight(id);
    if (!found) throw new Error("\u9AD8\u5149 ID \u4E0D\u5B58\u5728\uFF08\u53EF\u80FD\u5DF2\u5220\u9664\uFF09\u3002");
    const { filePath, anchor } = found;
    const target = anchor.kind === "pdf" && anchor.page ? `${filePath}#page=${anchor.page}` : filePath;
    await this.app.workspace.openLinkText(target, "", false);
    if (anchor.kind === "md") {
      const view = this.app.workspace.getActiveViewOfType(MarkdownView);
      if (view?.file?.path === filePath && view.editor) {
        const span = editorAnchorSpan(view.editor.getValue(), anchor);
        if (span) {
          const from = view.editor.offsetToPos(span.from);
          const to = view.editor.offsetToPos(span.to);
          view.editor.setSelection(from, to);
          view.editor.scrollIntoView?.({ from, to }, true);
          return;
        }
      }
      new Notice("\u5DF2\u6253\u5F00\u6E90\u6587\u6863\uFF0C\u4F46\u539F\u6587\u4F4D\u7F6E\u65E0\u6CD5\u552F\u4E00\u786E\u5B9A\u3002");
    }
  }
  pdfLeaves() {
    try {
      return this.app.workspace.getLeavesOfType("pdf") || [];
    } catch (_error) {
      return [];
    }
  }
  pdfContainers() {
    const found = [];
    for (const leaf of this.pdfLeaves()) {
      const view = leaf && leaf.view;
      const file = view && view.file;
      const container = view && view.containerEl;
      if (file && container) found.push({ container, filePath: file.path });
    }
    return found;
  }
  scheduleHighlightRefresh(delay = 80) {
    if (this.pdfRefreshTimer) {
      if (delay >= 80) return;
      clearTimeout(this.pdfRefreshTimer);
    }
    this.pdfRefreshTimer = setTimeout(() => {
      this.pdfRefreshTimer = null;
      this.refreshHighlights();
    }, delay);
  }
  refreshHighlights() {
    const registry = highlightRegistry();
    if (!registry) return;
    const refreshStartedAt = performance.now();
    this.lastRangeError = "";
    const rangesByColor = /* @__PURE__ */ new Map();
    this.interactiveRanges = [];
    const notes = [];
    let awaitingRender = false;
    for (const { container, filePath } of this.pdfContainers()) {
      if (!container.isConnected) {
        notes.push(`${filePath}\uFF1A\u5BB9\u5668\u5DF2\u65AD\u5F00`);
        continue;
      }
      try {
        if (this.collectPdfRanges(container, filePath, rangesByColor, notes)) {
          awaitingRender = true;
        }
      } catch (error) {
        this.lastRangeError = `pdf \u6536\u96C6\uFF1A${error && error.message ? error.message : error}`;
      }
    }
    for (const surface of this.readingSurfaces) {
      if (!surface.element.isConnected) {
        this.readingSurfaces.delete(surface);
        continue;
      }
      this.collectReadingRanges(surface, rangesByColor);
    }
    for (const name of this.activeHighlightRegistryNames || HIGHLIGHT_COLORS.map(highlightRegistryName))
      registry.delete(name);
    this.activeHighlightRegistryNames = new Set(rangesByColor.keys());
    this.collectNotes = notes;
    let collected = 0;
    for (const [name, ranges] of rangesByColor) {
      if (!ranges.length) continue;
      collected += ranges.length;
      try {
        registry.set(name, new Highlight(...ranges));
      } catch (error) {
        this.lastRangeError = `${name}\uFF1A${error && error.message ? error.message : error}`;
      }
    }
    for (const item of this.interactiveRanges || []) {
      if (item.id) this.signalPaintProbe(item.id, "pdf-or-reading", () => {
        const anchor = this.findHighlight(item.id)?.anchor;
        const name = highlightRegistryName(renderedColor(anchorColorHex(anchor)));
        return !!anchor && !!registry.get(name)?.size;
      });
    }
    this.lastRangeCount = collected;
    this.lastHighlightRefreshMs = Math.round((performance.now() - refreshStartedAt) * 10) / 10;
    if (collected > 0) {
      this.highlightRetryCount = 0;
    } else if (awaitingRender) {
      this.scheduleHighlightRetry();
    }
  }
  scheduleHighlightRetry() {
    if (this.highlightRetryTimer) return;
    if ((this.highlightRetryCount || 0) >= 3) return;   // 卡住就别死循环
    this.highlightRetryCount = (this.highlightRetryCount || 0) + 1;
    this.highlightRetryTimer = setTimeout(() => {
      this.highlightRetryTimer = null;
      this.refreshHighlights();
    }, 300);
  }

  collectReadingRanges({ element, filePath }, rangesByColor) {
    const anchors = this.anchorsForFile(filePath)
      .filter((anchor) => anchor.kind === "md" && anchor.exact);
    if (!anchors.length) return;
    const { text, parts } = readTextNodes(element);
    if (!text) return;
    const makeRange = (element.ownerDocument || document).createRange.bind(
      element.ownerDocument || document,
    );
    for (const anchor of anchors) {
      // 走**和重锚定同一套阶梯**，而不是只拿 exact 去比对。
      // 只比对 exact 是过度保守：锚点里存着前后最多 64 字符的上下文，明明能把
      // 重复片段区分开，却因为「出现多次」整条跳过。用户实测时就撞上这个——
      // 他在索引文档里划词，而索引条目本身会重复引用同一段文字。
      const hit = resolveAnchor(text, anchor);
      if (!hit) continue;                     // 真定不下来才跳过，不猜
      const ranges = rangesForSlices(
        slicesForRange(parts, hit.start, hit.end), makeRange,
      );
      if (!ranges.length) continue;
      const name = highlightRegistryName(renderedColor(anchorColorHex(anchor)));
      if (!rangesByColor.has(name)) rangesByColor.set(name, []);
      rangesByColor.get(name).push(...ranges);
      if (anchor.id) for (const range of ranges) {
        this.interactiveRanges.push({ filePath, id: anchor.id, range, root: element });
      }
    }
  }

  registerReadingViewHighlights() {
    if (!highlightRegistry()) return;
    this.registerMarkdownPostProcessor((element, context) => {
      if (!context || !context.sourcePath) return;
      // 无锚点的初次阅读视图也要登记；否则随后第一条高光保存时没有载体可画。
      this.readingSurfaces.add({ element, filePath: context.sourcePath });
      // 后处理器是同步跑的，注册完排在下一轮再画：这一块的内容这会儿才刚进 DOM
      this.scheduleHighlightRefresh();
    });
  }

  // Markdown 编辑视图交由 CodeMirror 装饰；仅阅读视图与 PDF 使用 DOM Range。
  markdownContentRoots() {
    const roots = [];
    for (const leaf of this.app.workspace.getLeavesOfType("markdown") || []) {
      const view = leaf && leaf.view;
      if (view && view.file && view.editor && view.editor.cm) {
        roots.push({ filePath: view.file.path, cm: view.editor.cm });
      }
    }
    return roots;
  }

  handleEditorUpdate(update) {
    if (!update.docChanged) return;
    const oldMarks = update.startState.field(editorMarks, false);
    const newMarks = update.state.field(editorMarks, false);
    if (!oldMarks || !newMarks) return;
    const affected = affectedEditorMarkIds(oldMarks, update.changes);
    const surface = this.markdownContentRoots().find(({ cm }) => cm === update.view);
    if (!surface) return;
    const content = update.state.doc.toString();
    const mapped = new Map(marksFromSet(newMarks).filter((mark) => mark.id)
      .map((mark) => [mark.id, mark]));
    const anchors = this.highlightStore?.files?.[surface.filePath] || [];
    // 同一事务里更新所有已映射锚点：即使只是高光前插字，也必须持久化新偏移。
    // 重复片段的原偏移不能留到下一次打开文件再用上下文猜测。
    for (const anchor of [...anchors]) {
      if (anchor.kind !== "md" || !anchor.id || this.deletingHighlights?.has(anchor.id)) continue;
      const mark = mapped.get(anchor.id);
      if (!mark) {
        if (!affected.has(anchor.id)) continue;
        this.highlightStore.files[surface.filePath] =
          this.highlightStore.files[surface.filePath].filter((item) => item.id !== anchor.id);
        this.highlightById?.delete(anchor.id);
        this.updateOpenAnnotation(surface.filePath, null, anchor.id);
        this.queueAnchorEdit(surface.filePath, null, anchor.id);
        continue;
      }
      const quote = describeQuote(content, mark.from, mark.to);
      if (!quote.exact) continue;
      if (anchor.exact === quote.exact && anchor.start === mark.from
          && anchor.end === mark.to && anchor.prefix === quote.prefix
          && anchor.suffix === quote.suffix) continue;
      const updated = { ...anchor, start: mark.from, end: mark.to, ...quote };
      const index = this.highlightStore.files[surface.filePath]
        .findIndex((item) => item.id === anchor.id);
      if (index >= 0) this.highlightStore.files[surface.filePath][index] = updated;
      this.highlightById?.set(anchor.id, { filePath: surface.filePath, anchor: updated });
      this.queueAnchorEdit(surface.filePath, updated, anchor.id);
    }
    if (affected.size || update.docChanged) this.scheduleHighlightSidebarRefresh?.();
  }

  scheduleEditorDecorationRefresh() {
    if (this.editorRefreshTimer) clearTimeout(this.editorRefreshTimer);
    this.editorRefreshTimer = setTimeout(() => {
      this.editorRefreshTimer = null;
      this.refreshEditorDecorations();
    }, 0);
  }

  refreshEditorDecorations() {
    const started = typeof performance !== "undefined" ? performance.now() : Date.now();
    let refreshed = 0;
    for (const { filePath, cm } of this.markdownContentRoots()) {
      if (!cm.state.field(editorMarks, false)) continue;
      try {
        const marks = editorMarksForDocument(cm.state.doc.toString(), this.anchorsForFile(filePath));
        cm.dispatch({ effects: replaceEditorMarks.of(marks) });
        refreshed += 1;
      } catch (error) {
        this.lastEditorError = `编辑器装饰：${error.message || error}`;
      }
    }
    this.lastEditorFullRefreshMs = Math.round(((typeof performance !== "undefined"
      ? performance.now() : Date.now()) - started) * 100) / 100;
    this.lastEditorFullRefreshCount = refreshed;
  }

  pushEditorAnchor(filePath, anchor, traceSteps = null) {
    if (anchor.kind !== "md") return;
    if (traceSteps) traceSteps.editor_lookup_start_wall_ms = Date.now();
    for (const surface of this.markdownContentRoots()) {
      if (surface.filePath !== filePath) continue;
      const { cm } = surface;
      if (!cm.state.field(editorMarks, false)) continue;
      const span = editorAnchorSpan(cm.state.doc.toString(), anchor);
      if (span) {
        if (traceSteps) traceSteps.editor_dispatch_start_wall_ms = Date.now();
        cm.dispatch({ effects: appendEditorMark.of({
          ...span, color: renderedColor(anchorColorHex(anchor)), id: anchor.id,
        }) });
        if (traceSteps) traceSteps.editor_dispatch_end_wall_ms = Date.now();
        if (anchor.id) this.signalPaintProbe(anchor.id, "markdown",
          () => !!cm.contentDOM?.querySelector('[data-nh-highlight-id="' + anchor.id + '"]'));
      }
    }
    if (traceSteps) traceSteps.editor_lookup_end_wall_ms = Date.now();
  }

  // 一条 PDF 锚点现在卡在哪一步。四种结果一一对应四种毛病，看一句就知道该修哪：
  // 没滚到那页 / 那页没渲染 / 渲染了但文字对不上 / 命中。
  pdfAnchorStatus(container, anchor) {
    const pageEl = pdfPageNumbers(container).find(
      (element) => Number.parseInt(element.getAttribute("data-page-number"), 10)
        === anchor.page,
    );
    if (!pageEl) return "这一页不在 DOM 里";
    const layerEl = pageEl.querySelector(".textLayer");
    if (!layerEl) return "这一页还没渲染出文本层（滚到那页再看）";
    const layer = readPageText(textNodesOf(layerEl));
    const hit = locateInPage(layer.text, anchor.exact, anchor.occurrence);
    if (hit) return `命中（层内第 ${hit.start}..${hit.end} 字）`;
    return `文本层在，但文字对不上（层内 ${layer.text.length} 字）`;
  }

  // 返回值：是否有锚点因为「页面的文本层还没渲染出来」被跳过。用返回值传，
  // **不要靠匹配 notes 里的文案**——那句话一改，自愈就静默失效了（踩过）。
  collectPdfRanges(container, filePath, rangesByColor, notes) {
    let awaitingRender = false;
    // 每一步都留一句「为什么跳过」。这条链上任何一环无声失败，从外面看都只是
    // 「没颜色」——分不出是哪一环，只能一轮一轮试。踩过太多次，索性一次说清。
    // notes 由调用方传进来（不要挂到 this 上再被覆盖掉，那样插桩会丢）。
    const anchors = this.anchorsForFile(filePath).filter(
      (anchor) => anchor.kind === "pdf" && Number.isFinite(anchor.page),
    );
    if (!anchors.length) {
      notes.push(`${filePath}：没有 pdf 锚点`);
      return false;
    }
    // 同页码出现两次时**取第一个**——必须和 pdfAnchorStatus 的 .find() 一致。
    // 原先 Map 循环是「最后一个赢」，两处就可能看到不同的节点（一个带文本层、
    // 一个不带），于是同一时刻一份报告里出现「命中」和「没渲染出文本层」。
    const pages = new Map();
    for (const element of pdfPageNumbers(container)) {
      const number = Number.parseInt(element.getAttribute("data-page-number"), 10);
      if (Number.isInteger(number) && !pages.has(number)) pages.set(number, element);
    }
    const layers = new Map();            // 页号 -> {text, parts}，一页只算一次
    const makeRange = container.ownerDocument.createRange.bind(container.ownerDocument);
    for (const anchor of anchors) {
      const label = `p.${anchor.page}`;
      const pageEl = pages.get(anchor.page);
      if (!pageEl) {
        notes.push(`${label}：这一页不在 DOM 里`);
        awaitingRender = true;
        continue;
      }
      let layer = layers.get(anchor.page);
      if (layer === undefined) {
        const layerEl = pageEl.querySelector(".textLayer");
        layer = layerEl ? readPageText(textNodesOf(layerEl)) : null;
        layers.set(anchor.page, layer);
      }
      if (!layer) {
        const kind = pageEl.tagName ? pageEl.tagName.toLowerCase() : "?";
        const kids = pageEl.children ? pageEl.children.length : -1;
        const hasLayer = pageEl.querySelector ? !!pageEl.querySelector(".textLayer") : false;
        notes.push(
          `${label}：没有 .textLayer（页元素 <${kind} class="${String(pageEl.className || "").slice(0, 40)}"> `
          + `子元素 ${kids} 个，再查一次=${hasLayer}）`,
        );
        awaitingRender = true;
        continue;
      }
      const hit = locateInPage(layer.text, anchor.exact, anchor.occurrence);
      if (!hit) {
        notes.push(`${label}：文字对不上（层内 ${layer.text.length} 字）`);
        continue;
      }
      const slices = slicesForRange(layer.parts, hit.start, hit.end);
      const ranges = rangesForSlices(slices, makeRange);
      if (!ranges.length) {
        notes.push(`${label}：命中但切不出 Range（slices=${slices.length}）`);
        continue;
      }
      notes.push(`${label}：上色 ${ranges.length} 段`);
      const name = highlightRegistryName(renderedColor(anchorColorHex(anchor)));
      if (!rangesByColor.has(name)) rangesByColor.set(name, []);
      rangesByColor.get(name).push(...ranges);
      if (anchor.id) for (const range of ranges) {
        this.interactiveRanges.push({ filePath, id: anchor.id, range, root: container });
      }
    }
    return awaitingRender;
  }

  // PDF/阅读视图的渲染根以外的侧栏、Markdown 编辑器、悬浮提示等 DOM
  // 变化不需要重算所有 Range。仍观察稳定的 body，以捕捉容器整体替换。
  highlightMutationRoots() {
    const roots = this.pdfContainers().filter(({ filePath }) => this.anchorsForFile(filePath)
      .some((anchor) => anchor.kind === "pdf")).map(({ container }) => container);
    for (const surface of this.readingSurfaces) {
      if (!surface.element.isConnected) {
        this.readingSurfaces.delete(surface);  // 虚拟化替换的旧块不再参与每次过滤
      } else if (this.anchorsForFile(surface.filePath).some((anchor) => anchor.kind === "md")) {
        roots.push(surface.element);
      }
    }
    return roots;
  }

  // PDF 的文本层会被重建，编辑视图的内容会随滚动/输入变化（虚拟化只保留可见行），
  // 所以两类容器都要盯着，变了就重画。改动很频繁，全靠 scheduleHighlightRefresh
  // 的合并（80 ms）压住。
  watchHighlightSurfaces() {
    if (!highlightRegistry()) return;    // 取不到 Highlight API 就不装这一套
    if (this.pdfObserver) this.pdfObserver.disconnect();
    const observer = new MutationObserver((mutations) => {
      if (mutationsTouchHighlightRoots(mutations, this.highlightMutationRoots())) {
        this.scheduleHighlightRefresh();
      }
    });
    this.pdfObserver = observer;
    // 观察**稳定的根**，不观察每个 leaf 的 containerEl。
    //
    // 踩过：观察器原来挂在 `view.containerEl` 上。PDF 的文本层渲染完时，如果
    // 那个容器已经被换掉（分区重建、切文件、翻页虚拟化都可能），观察器就挂在
    // 一个废弃节点上，**永远收不到 mutation**，于是再也不会重画。表现出来是
    // 一个很难解释的现象：诊断里同一条锚点在 `pdfAnchorStatus` 里「命中」，
    // 在收集那一步却是「还没渲染出文本层」——因为前者是命令执行时才查的，
    // 后者是几十毫秒前的快照，而这中间的插入事件没人听见。
    //
    // body + childList + subtree 足够：PDF 渲染是添节点，编辑器的滚动虚拟化与
    // 输入也会替换行元素（characterData 不必开，省掉大量无谓触发）。
    const root = (this.app.workspace.containerEl
      && this.app.workspace.containerEl.ownerDocument
      && this.app.workspace.containerEl.ownerDocument.body) || document.body;
    if (root) observer.observe(root, { childList: true, subtree: true });
    this.scheduleHighlightRefresh();
  }

  // 同一处文字已经高光过就不要再叠一条：两条锚点会画出两层颜色，而且用户
  // 点色块时看不出「其实已经设过了」。改色是下一阶段的事。
  findAnchorByCapture(captureId) {
    if (!captureId || !this.highlightStore || !this.highlightStore.files) return null;
    for (const [filePath, anchors] of Object.entries(this.highlightStore.files)) {
      const anchor = anchors.find((item) => item.captureId === captureId);
      if (anchor) return { filePath, anchor };
    }
    return null;
  }

  findOverlappingAnchor(filePath, anchor) {
    const anchors = this.anchorsForFile(filePath);
    for (const existing of anchors) {
      if (existing.exact !== anchor.exact) continue;
      if (anchor.kind === "pdf") {
        if (existing.kind === "pdf" && existing.page === anchor.page
            && existing.occurrence === anchor.occurrence) return existing;
        continue;
      }
      if (existing.kind !== "md") continue;
      if (Number.isFinite(existing.start) && Number.isFinite(anchor.start)
          && existing.start === anchor.start) return existing;
    }
    return null;
  }

  async handleRequest(request, response) {
    const requestReceivedWallMs = Date.now();
    if (!this.isAuthorized(request)) {
      sendJson(response, 401, { error: "unauthorized" });
      return;
    }
    if (request.method === "GET" && request.url === "/desktop-mode") {
      await this.waitForDesktopMode(response);
      return;
    }
    if (request.method === "GET" && request.url === "/highlight-diagnostics") {
      const editors = this.markdownContentRoots().map(({ filePath, cm }) => {
        try {
          const field = cm.state.field(editorMarks, false);
          return {
            file_path: filePath,
            extension_loaded: !!field,
            anchor_count: this.anchorsForFile(filePath).filter((a) => a.kind === "md").length,
            decoration_count: field ? field.size : 0,
            visible_marks: cm.contentDOM?.querySelectorAll("[data-nh-highlight-id]").length || 0,
          };
        } catch (error) {
          return { file_path: filePath, error: String(error.message || error) };
        }
      });
      sendJson(response, 200, {
        version: this.manifest.version,
        editors,
        active_highlights: this.activeHighlightCount(),
        source_files: Object.keys(this.highlightStore?.files || {}).length,
        resetting: !!this.highlightResetting,
        journal_error: this.highlightStoreError || "",
        migration_warning: this.lastMigrationWarning || "",
        editor_error: this.lastEditorError || "",
        selection_error: this.lastSelectionError || "",
        anchor_edit_error: this.lastAnchorEditError || "",
        pending_anchor_edits: this.pendingAnchorEdits?.size || 0,
        editor_full_refresh_ms: this.lastEditorFullRefreshMs ?? null,
        editor_full_refresh_count: this.lastEditorFullRefreshCount ?? 0,
        pipe_error: this.lastPipeError || "",
      });
      return;
    }
    if (request.method === "GET" && request.url === "/health") {
      sendJson(response, 200, {
        ok: true, vault: this.app.vault.getName(), version: this.manifest.version,
        cross_vault_export: true,
        highlight_retry: true, last_highlight_ms: this.lastHighlightMs || 0,
        pipe_available: !!this.pipeServer?.listening,
        pipe_persistent: true,
        highlight_colors: DEFAULT_HIGHLIGHT_PALETTE,
        active_highlight_colors: this.activeHighlightColors(),
        native_highlight_entry: this.settings.nativeHighlightEntry,
        storage: this.storagePaths(),
      });
      return;
    }
    // 桌面端「按颜色导出」菜单的取数口。必须排在下面那条 POST 守卫之前——放后面
    // GET 会直接落到 404，而且只有真跑一次菜单才发现。
    if (request.method === "GET" && request.url === "/highlight-export-options") {
      sendJson(response, 200, { ok: true, version: this.manifest.version,
        ...this.highlightExportOptions() });
      return;
    }
    if (request.method !== "POST") {
      sendJson(response, 404, { error: "not found" });
      return;
    }
    const payload = await readJson(request);
    if (payload.trace === true && request._nhPipeTrace) payload._pipeTrace = request._nhPipeTrace;
    if (request.url === "/highlight-reset") {
      if (payload.confirm !== "CLEAR_ALL_HIGHLIGHTS") {
        sendJson(response, 400, { error: "须明确确认清空所有高光。" });
        return;
      }
      try { sendJson(response, 200, await this.resetHighlights(payload.expected_count)); }
      catch (error) { sendJson(response, 409, { error: String(error.message || error) }); }
      return;
    }
    if (request.url === "/highlight-entry") {
      try { sendJson(response, 200, {
        enabled: await this.configureNativeHighlightEntry(payload.enabled),
      }); }
      catch (error) { sendJson(response, 400, { error: String(error.message || error) }); }
      return;
    }
    if (request.url === "/highlight-colors") {
      try { sendJson(response, 200, { colors: await this.configureHighlightColors(payload.colors), fixed: true }); }
      catch (error) { sendJson(response, 400, { error: String(error.message || error) }); }
      return;
    }
    if (request.url === "/highlight-single-export-config") {
      try { sendJson(response, 200, { config: await this.configureHighlightSingleExport(payload) }); }
      catch (error) { sendJson(response, 400, { error: String(error.message || error) }); }
      return;
    }
    if (request.url === "/highlight-active-colors") {
      try { sendJson(response, 200, { colors: await this.configureHighlightActiveColors(payload) }); }
      catch (error) { sendJson(response, 400, { error: String(error.message || error) }); }
      return;
    }
    if (request.url === "/highlight-export") {
      const color = payload.color === undefined || payload.color === null ? "" : String(payload.color);
      if (color && !HIGHLIGHT_COLORS.includes(color)) {
        sendJson(response, 400, { error: "颜色范围无效。" });
        return;
      }
      try { sendJson(response, 200, await this.writeHighlightExport(payload.directory, { color, vaultPath: payload.vault_path || "" })); }
      catch (error) { sendJson(response, 500, { error: String(error.message || error) }); }
      return;
    }
    if (request.url === "/capture") {
      await this.captureSelection(response);
      return;
    }
    if (request.url === "/anchor") {
      await this.createAnchor(payload.capture_id, response);
      return;
    }
    if (request.url === "/highlight") {
      const captureId = String(payload.capture_id || "");
      if (!this.highlightInFlight) this.highlightInFlight = new Map();
      const running = this.highlightInFlight.get(captureId);
      if (running) {
        // 第一个 HTTP 请求可能已超时但仍在写盘；等它结束后重放结果，
        // 而不是并发创建第二条锚点。
        // 首次请求的 socket 可能已断，回包异常不代表写盘失败；
        // 再查持久化 captureId，未落盘时仍可用同一 capture 安全重做。
        await running.catch(() => {});
        await this.applyHighlight(payload, response);
      } else {
        const started = Date.now();
        if (payload.trace === true) payload._receivedWallMs = requestReceivedWallMs;
        const operation = this.applyHighlight(payload, response);
        this.highlightInFlight.set(captureId, operation);
        try {
          await operation;
        } finally {
          this.highlightInFlight.delete(captureId);
          this.lastHighlightMs = Date.now() - started;
        }
      }
      return;
    }
    sendJson(response, 404, { error: "not found" });
  }

  captureCurrentSelection() {
    const activeFile = this.app.workspace.getActiveFile();
    if (!(activeFile instanceof TFile)) return { error: "Obsidian 中没有活动文件。", status: 409 };
    const extension = activeFile.extension.toLowerCase();
    if (extension === "pdf") {
      const container = this.app.workspace.activeLeaf?.view?.containerEl;
      const highlight = container ? pdfAnchorFromSelection(container) : null;
      if (!highlight?.exact || highlight.page_number == null)
        return { error: "没有取到 PDF 页码或页内文字，请重新划选。", status: 409 };
      const captureId = crypto.randomBytes(12).toString("hex");
      this.purgeExpiredCaptures();
      this.captures.set(captureId, {
        kind: "file", filePath: activeFile.path, selection: highlight.exact,
        startOffset: -1, endOffset: -1, pageNumber: highlight.page_number,
        occurrence: highlight.occurrence || 0, createdAt: Date.now(),
      });
      return { capture_id: captureId, file_path: activeFile.path, kind: "pdf" };
    }
    if (extension !== "md") return { error: "高光只支持 Markdown 与 PDF。", status: 409 };
    const view = this.app.workspace.getActiveViewOfType(MarkdownView);
    if (!view?.file || view.file.path !== activeFile.path)
      return { error: "无法读取当前 Obsidian Markdown 视图。", status: 409 };
    const { selection, startOffset, endOffset } = markdownViewSelection(view);
    if (!selection.trim()) return { error: "Obsidian 中没有有效选区。", status: 409 };
    const captureId = crypto.randomBytes(12).toString("hex");
    this.purgeExpiredCaptures();
    this.captures.set(captureId, {
      filePath: view.file.path, selection, startOffset, endOffset, createdAt: Date.now(),
    });
    return { capture_id: captureId, file_path: view.file.path, kind: "md" };
  }

  async configureNativeHighlightEntry(enabled) {
    if (typeof enabled !== "boolean") throw new Error("高光入口开关必须是布尔值。");
    // Serialize persisted mode updates; freeze the old entry during handoff.
    this.nativeEntryTransitionCount = (this.nativeEntryTransitionCount || 0) + 1;
    this.hideNativePalette();
    const operation = (this.nativeEntryQueue || Promise.resolve()).catch(() => {}).then(async () => {
      if (this.settings.nativeHighlightEntry !== enabled) {
        await this.saveData({ ...this.settings, nativeHighlightEntry: enabled });
        this.settings.nativeHighlightEntry = enabled;
      }
      this.nativeModeHandoff = false;
      return enabled;
    }).finally(() => { this.nativeEntryTransitionCount -= 1; });
    this.nativeEntryQueue = operation;
    return operation;
  }

  hideNativePalette() {
    this.nativePalette?.remove();
    this.nativePalette = null;
    this.nativeCaptureId = null;
  }

  finishDesktopModeWait(mode) {
    const waiter = this.desktopModeWaiter;
    if (!waiter) return false;
    this.desktopModeWaiter = null;
    clearTimeout(waiter.timer);
    waiter.response.off?.("close", waiter.closed);
    sendJson(waiter.response, 200, { mode });
    waiter.resolve();
    return true;
  }

  requestDesktopMode(mode) {
    if (!["question", "translate", "highlight"].includes(mode)) return false;
    return this.finishDesktopModeWait(mode);
  }

  async waitForDesktopMode(response) {
    // A pending HTTP reply is a one-shot event subscription, not a 2s polling loop.
    if (this.desktopModeWaiter) {
      sendJson(response, 409, { error: "已有桌面端在等待模式事件。" });
      return;
    }
    await new Promise((resolve) => {
      const waiter = { response, resolve, timer: null, closed: null };
      waiter.closed = () => {
        if (this.desktopModeWaiter === waiter) this.desktopModeWaiter = null;
        clearTimeout(waiter.timer);
        resolve();
      };
      waiter.timer = setTimeout(() => this.finishDesktopModeWait(null), 15000);
      response.on?.("close", waiter.closed);
      this.desktopModeWaiter = waiter;
    });
  }

  switchFromNativePalette(mode) {
    if (mode === "highlight") return;
    if (!this.requestDesktopMode(mode)) {
      new Notice("桌面程序未连接；请在主页面切换模式。");
      return;
    }
    this.nativeModeHandoff = true;
    this.hideNativePalette();
  }

  onNativeSelectionEnd(event) {
    if (!this.settings.nativeHighlightEntry || this.nativeModeHandoff
        || this.nativeEntryTransitionCount || event.button > 0 || this.nativeSaving) return;
    const view = this.app.workspace.activeLeaf?.view;
    if (!view?.containerEl?.contains(event.target)) return;
    const file = this.app.workspace.getActiveFile();
    if (!(file instanceof TFile) || !["md", "pdf"].includes(file.extension.toLowerCase())) return;
    // The editor can retain an old selection even while the pointer is elsewhere in the pane.
    if (file.extension.toLowerCase() === "md" && view.getMode?.() !== "preview"
        && !view.editor?.cm?.contentDOM?.contains(event.target)) return;
    const capture = this.captureCurrentSelection();
    if (!capture.capture_id) { this.hideNativePalette(); return; }
    this.hideNativePalette();
    this.nativeCaptureId = capture.capture_id;
    const palette = document.createElement("div");
    palette.className = "nh-native-palette";
    palette.setAttribute("role", "group");
    palette.setAttribute("aria-label", "添加高光");
    const swatches = document.createElement("div");
    swatches.className = "nh-native-colors";
    // 桌面端配置页选的 3 种（8 选 3），不是全部 8 种。
    for (const color of this.activeHighlightColors()) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "nh-native-swatch";
      button.title = `添加${HIGHLIGHT_COLOR_DEFS[color].label}高光`;
      button.setAttribute("aria-label", button.title);
      button.style.backgroundColor = DEFAULT_HIGHLIGHT_PALETTE[color];
      button.addEventListener("mousedown", (down) => down.preventDefault());
      button.addEventListener("click", () => void this.addNativeHighlight(capture.capture_id, color));
      swatches.appendChild(button);
    }
    palette.appendChild(swatches);
    const modes = document.createElement("div");
    modes.className = "nh-native-modes";
    for (const [mode, label, icon] of [
      ["question", "记录问题", "pencil"],
      ["highlight", "高光模式", "palette"],
      ["translate", "记录单词", "feather"],
    ]) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = `nh-native-mode${mode === "highlight" ? " is-active" : ""}`;
      button.title = label;
      button.setAttribute("aria-label", label);
      button.setAttribute("aria-pressed", mode === "highlight" ? "true" : "false");
      if (icon === "feather") {
        const glyph = document.createElement("span");
        glyph.className = "nh-word-mode-icon";
        glyph.style.maskImage = "url(" + WORD_MODE_ICON + ")";
        glyph.style.webkitMaskImage = "url(" + WORD_MODE_ICON + ")";
        button.appendChild(glyph);
      } else setIcon(button, icon);
      button.addEventListener("mousedown", (down) => down.preventDefault());
      button.addEventListener("click", () => this.switchFromNativePalette(mode));
      modes.appendChild(button);
    }
    palette.appendChild(modes);
    const sidebarButton = document.createElement("button");
    sidebarButton.type = "button";
    sidebarButton.className = "nh-native-mode nh-native-sidebar";
    sidebarButton.title = "打开高光索引与注释侧栏";
    sidebarButton.setAttribute("aria-label", sidebarButton.title);
    setIcon(sidebarButton, "panel-right-open");
    sidebarButton.addEventListener("mousedown", (down) => down.preventDefault());
    sidebarButton.addEventListener("click", () => {
      this.hideNativePalette();
      void this.openHighlightSidebar().catch((error) => new Notice(String(error.message || error)));
    });
    swatches.appendChild(sidebarButton);
    document.body.appendChild(palette);
    const width = palette.offsetWidth || 156;
    const height = palette.offsetHeight || 94;
    const x = Math.min(Math.max(8, event.clientX), window.innerWidth - width - 8);
    const y = event.clientY + height + 12 > window.innerHeight
      ? event.clientY - height - 8 : event.clientY + 8;
    palette.style.left = `${Math.max(8, x)}px`;
    palette.style.top = `${Math.max(8, y)}px`;
    this.nativePalette = palette;
  }

  async addNativeHighlight(captureId, color) {
    if (!captureId || this.nativeCaptureId !== captureId || this.nativeSaving) return;
    this.nativeSaving = true;
    const palette = this.nativePalette;
    if (palette) palette.classList.add("is-saving");
    // Reuse the same validated, write-before-paint operation as the bridge route.
    const response = {
      destroyed: false, writableEnded: false, statusCode: 200, result: null,
      writeHead(status) { this.statusCode = status; },
      end(body) { this.result = JSON.parse(body || "{}"); this.writableEnded = true; },
    };
    try {
      await this.applyHighlight({ capture_id: captureId, color }, response);
      if (response.statusCode !== 200 || !response.result?.highlight_id)
        new Notice(`添加高光失败：${response.result?.error || "未知错误"}`);
    } catch (error) {
      new Notice(`添加高光失败：${error.message || error}`);
    } finally {
      this.nativeSaving = false;
      if (this.nativeCaptureId === captureId) this.hideNativePalette();
    }
  }

  async captureSelection(response) {
    const activeFile = this.app.workspace.getActiveFile();
    if (!(activeFile instanceof TFile)) {
      sendJson(response, 409, { error: "Obsidian 中没有活动文件。" });
      return;
    }
    if (activeFile.extension.toLowerCase() !== "md") {
      const activeView = this.app.workspace.activeLeaf
        ? this.app.workspace.activeLeaf.view
        : null;
      const containerEl = activeView && activeView.containerEl;
      const extension = activeFile.extension.toLowerCase();
      // PDF 的浮层锚点必须**在划词这一刻**取：此刻选区还在，页内文本层也渲染
      // 好了。等用户点悬浮窗时再取，选区多半已经没了（用户点的是我们自己的
      // 悬浮窗），页也可能被虚拟化回收。
      const highlight = extension === "pdf" && containerEl
        ? pdfAnchorFromSelection(containerEl)
        : null;
      const captureId = crypto.randomBytes(12).toString("hex");
      this.purgeExpiredCaptures();
      this.captures.set(captureId, {
        kind: "file",
        filePath: activeFile.path,
        selection: highlight ? highlight.exact : "",
        startOffset: -1,
        endOffset: -1,
        pageNumber: highlight ? highlight.page_number : null,
        occurrence: highlight ? highlight.occurrence : 0,
        createdAt: Date.now(),
      });
      sendJson(response, 200, {
        kind: "obsidian-file",
        capture_id: captureId,
        vault_name: this.app.vault.getName(),
        file_path: activeFile.path,
        file_name: activeFile.name,
        extension,
        page_number: highlight ? highlight.page_number : null,
        highlight,
      });
      return;
    }
    const view = this.app.workspace.getActiveViewOfType(MarkdownView);
    if (!view || !view.file) {
      sendJson(response, 409, { error: "无法读取当前 Obsidian Markdown 视图。" });
      return;
    }
    const { selection, startOffset, endOffset } = markdownViewSelection(view);
    if (!selection.trim()) {
      sendJson(response, 409, { error: "Obsidian 中没有有效选区。" });
      return;
    }
    const captureId = crypto.randomBytes(12).toString("hex");
    this.purgeExpiredCaptures();
    this.captures.set(captureId, {
      filePath: view.file.path,
      selection,
      startOffset,
      endOffset,
      createdAt: Date.now(),
    });
    sendJson(response, 200, {
      kind: "obsidian",
      capture_id: captureId,
      vault_name: this.app.vault.getName(),
      file_path: view.file.path,
      file_name: view.file.name,
      selection,
    });
  }

  async createAnchor(captureId, response) {
    this.purgeExpiredCaptures();
    const capture = this.captures.get(String(captureId || ""));
    if (!capture) {
      sendJson(response, 409, { error: "选区已过期，请重新划选原文。" });
      return;
    }
    const file = this.app.vault.getAbstractFileByPath(capture.filePath);
    if (!(file instanceof TFile) || file.extension.toLowerCase() !== "md") {
      sendJson(response, 409, { error: "来源 Markdown 已移动或删除。" });
      return;
    }
    const view = this.app.workspace.getActiveViewOfType(MarkdownView);
    const activeEditor = view && view.file && view.file.path === file.path ? view.editor : null;
    const content = activeEditor ? activeEditor.getValue() : await this.app.vault.read(file);
    const selection = findSelection(content, capture);
    if (!selection) {
      sendJson(response, 409, { error: "原文已变化或存在多个相同片段，请重新划选。" });
      return;
    }
    const blockEnd = blockEndOffset(content, selection.end);
    let anchor = existingBlockId(content, selection.start, blockEnd);
    if (!anchor) {
      anchor = `nh-${crypto.randomBytes(5).toString("hex")}`;
      const insertion = `${blockEnd > 0 && content[blockEnd - 1] === "\n" ? "" : "\n"}^${anchor}`;
      if (activeEditor) {
        activeEditor.replaceRange(insertion, activeEditor.offsetToPos(blockEnd));
      } else {
        const updated = content.slice(0, blockEnd) + insertion + content.slice(blockEnd);
        await this.app.vault.modify(file, updated);
      }
    }
    this.captures.delete(captureId);
    sendJson(response, 200, {
      kind: "obsidian",
      vault_name: this.app.vault.getName(),
      file_path: file.path,
      file_name: file.name,
      anchor,
    });
  }

  async applyHighlight(payload, response) {
    if (this.highlightResetting) {
      sendJson(response, 409, { error: "正在清空高光，请稍后重新划选。" });
      return;
    }
    const startedAt = Date.now();
    const captureId = String(payload.capture_id || "");
    // 持久化的 captureId 能在客户端超时、甚至插件重载后确认首次写入的结果。
    // 对同一次点击的重试直接返回已保存锚点，绝不重复上色。
    const completed = this.findAnchorByCapture(captureId);
    if (completed) {
      const file = this.app.vault.getAbstractFileByPath(completed.filePath);
      sendJson(response, 200, {
        kind: "obsidian", vault_name: this.app.vault.getName(),
        file_path: completed.filePath,
        file_name: file ? file.name : completed.filePath.split("/").pop(),
        highlight_id: completed.anchor.id,
        color: completed.anchor.color,
      });
      return;
    }
    const color = normalizeHighlightColor(payload.color);
    if (!color) {
      sendJson(response, 409, { error: "高光颜色无效。" });
      return;
    }
    this.purgeExpiredCaptures();
    const capture = this.captures.get(String(payload.capture_id || ""));
    if (!capture) {
      sendJson(response, 409, { error: "选区已过期，请重新划选原文。" });
      return;
    }
    const file = this.app.vault.getAbstractFileByPath(capture.filePath);
    if (!(file instanceof TFile)) {
      sendJson(response, 409, { error: "来源文件已移动或删除。" });
      return;
    }

    // **这一整条路径不修改源文件。** 高亮锚点追加到插件目录的按文件日志，
    // 编辑视图用 CodeMirror 装饰，阅读/PDF 用 DOM Range。上一版把 <mark> 写进
    // 正文的：Obsidian 的实时预览会在光标所在行露出那串带属性的 HTML，很难看，
    // 而且后续「点高光弹注释」还得去解析 HTML 属性。用户实测后否掉了那条路。
    let anchor;
    if (capture.kind === "file") {
      if (file.extension.toLowerCase() !== "pdf") {
        sendJson(response, 409, {
          error: `高光暂不支持 Vault 里的 .${file.extension} 文件，只支持 Markdown 与 PDF。`,
        });
        return;
      }
      if (capture.pageNumber == null || !capture.selection) {
        sendJson(response, 409, {
          error: "没有取到 PDF 页码或页内文字，请重新划选。",
        });
        return;
      }
      anchor = {
        kind: "pdf",
        page: capture.pageNumber,
        exact: capture.selection,
        occurrence: capture.occurrence || 0,
      };
    } else {
      const view = this.app.workspace.getActiveViewOfType(MarkdownView);
      const activeEditor = view && view.file && view.file.path === file.path
        ? view.editor : null;
      const content = activeEditor
        ? activeEditor.getValue()
        : await this.app.vault.read(file);
      const selection = findSelection(content, capture);
      if (!selection) {
        // 把原因说具体：出现几次、偏移还可不可用。一句笼统的「原文已变化」
        // 让用户和开发者都无从下手。
        const count = allIndices(content, capture.selection).length;
        const offsetHint = capture.startOffset >= 0
          ? "" : "（划词时没记下位置，无法在重复片段里挑）";
        sendJson(response, 409, {
          error: count > 1
            ? `这段文字在文档里出现 ${count} 次，且位置无法确定，请重新划选。`
            : `原文已变化，请重新划选。${offsetHint}`,
        });
        return;
      }
      anchor = {
        kind: "md",
        start: selection.start,
        end: selection.end,
        ...describeQuote(content, selection.start, selection.end),
      };
    }

    if (this.findOverlappingAnchor(file.path, anchor)) {
      sendJson(response, 409, { error: "这段文字已经是高光，改色还没有做。" });
      return;
    }

    anchor.id = `hl-${crypto.randomBytes(12).toString("hex")}`;
    anchor.captureId = captureId;
    anchor.color = color;
    anchor.colorHex = DEFAULT_HIGHLIGHT_PALETTE[color];
    anchor.createdAt = Date.now();
    const clearSourceSelection = () => {
      const active = this.app.workspace?.getActiveFile?.();
      if (active?.path !== file.path) return;
      if (capture.kind === "file") {
        for (const surface of this.pdfContainers()) {
          if (surface.filePath === file.path
              && clearDomSelectionInside(surface.container, capture.selection)) break;
        }
      } else {
        const view = this.app.workspace.getActiveViewOfType(MarkdownView);
        if (view?.file?.path !== file.path) return;
        if (view.getMode?.() === "preview") clearDomSelectionInside(view.containerEl, capture.selection);
        else clearEditorSelectionIfPainted(view, anchor);
      }
    };
    const paintProbe = payload.trace === true ? this.armPaintProbe(anchor.id) : null;
    const traceSteps = payload.trace === true ? {} : null;
    let storageCommitWallMs = null;
    try {
      storageCommitWallMs = await this.addAnchor(file.path, anchor, clearSourceSelection, traceSteps);
    } catch (error) {
      this.pendingPaintProbes?.delete(anchor.id);
      sendJson(response, 500, {
        error: `写入高光数据失败：${error && error.message ? error.message : error}`,
      });
      return;
    }
    this.captures.delete(String(payload.capture_id || ""));
    const paint = await this.finishPaintProbe(paintProbe);
    sendJson(response, 200, {
      kind: "obsidian",
      vault_name: this.app.vault.getName(),
      file_path: file.path,
      file_name: file.name,
      highlight_id: anchor.id,
      color,
      plugin_ms: Date.now() - startedAt,
      storage_ms: this.lastHighlightWriteMs || 0,
      storage_mkdir_ms: this.lastHighlightMkdirMs || 0,
      storage_append_ms: this.lastHighlightAppendMs || 0,
      ...(payload.trace === true ? { trace: {
        request_received_wall_ms: payload._receivedWallMs || startedAt,
        storage_commit_wall_ms: storageCommitWallMs,
        ...payload._pipeTrace,
        ...traceSteps,
        ...paint,
      } } : {}),
    });
  }
}

module.exports = NotingHelperBridge;
module.exports._test = {
  activePdfPageNumber,
  anchorsInRange,
  blockEndOffset,
  describeQuote,
  existingBlockId,
  findSelection,
  highlightTimeLabel,
  touchesAnnotation,
  isPlainLeftClick,
  linkElementFromEvent,
  locateInPage,
  markdownViewSelection,
  mergeIndexRecord,
  HIGHLIGHT_COLORS,
  HIGHLIGHT_COLOR_DEFS,
  DEFAULT_HIGHLIGHT_PALETTE,
  ACTIVE_COLOR_COUNT,
  DEFAULT_ACTIVE_COLORS,
  normalizeActiveColors,
  annotationTitle,
  tagMarkup,
  highlightColorDef,
  highlightColorKey,
  anchorColorHex,
  anchorColorLabel,
  anchorColorDot,
  normalizeHighlightColor,
  normalizeWithMap,
  occurrenceForOffset,
  pdfAnchorFromSelection,
  mutationsTouchHighlightRoots,
  parseWikiLinkAtOffset,
  originalLinkInfo,
  pageNumberFromSelection,
  normalizeLoose,
  rangesForSlices,
  readPageText,
  renderedColor,
  resolveAnchor,
  sourceHasRepeatedExact,
  slicesForRange,
  editorAnchorSpan,
  editorMarksForDocument,
  affectedEditorMarkIds,
  editorMarks,
  replaceEditorMarks,
  appendEditorMark,
  highlightPipeName,
  EditorState,
  clearDomSelectionInside,
  clearEditorSelectionIfPainted,
  HighlightSidebarView,
};
