const assert = require("assert");
const Module = require("module");
const path = require("path");
const { Readable } = require("stream");
const FakeTFile = class TFile {};

const originalLoad = Module._load;
Module._load = function (request, parent, isMain) {
  if (request === "obsidian") {
    return {
      MarkdownView: class MarkdownView {},
      ItemView: class ItemView {},
      setIcon: () => {},
      Notice: class Notice {},
      Plugin: class Plugin {},
      TFile: FakeTFile,
    };
  }
  return originalLoad.call(this, request, parent, isMain);
};

const PluginClass = require(path.join(
  __dirname,
  "..",
  "obsidian-plugin",
  "noting-helper-bridge",
  "main.js"
));
// Obsidian 内建的 CodeMirror 必须与插件使用同一份模块实例；禁止再次打包。
const builtPlugin = require("fs").readFileSync(path.join(
  __dirname, "..", "obsidian-plugin", "noting-helper-bridge", "main.js",
), "utf8");
assert.ok(builtPlugin.includes('require("@codemirror/state")'));
assert.ok(builtPlugin.includes('require("@codemirror/view")'));

const {
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
  normalizeHighlightColor,
  normalizeWithMap,
  renderedColor,
  occurrenceForOffset,
  parseWikiLinkAtOffset,
  originalLinkInfo,
  readPageText,
  resolveAnchor,
  sourceHasRepeatedExact,
  slicesForRange,
  pageNumberFromSelection,
  editorAnchorSpan,
  editorMarksForDocument,
  editorMarks,
  replaceEditorMarks,
  appendEditorMark,
  EditorState,
  clearDomSelectionInside,
  clearEditorSelectionIfPainted,
} = PluginClass._test;

const content = "第一段第一行\n第一段第二行\n\n第二段\n";
const selected = "第一段第二行";
const start = content.indexOf(selected);
const capture = {
  selection: selected,
  startOffset: start,
  endOffset: start + selected.length,
};
assert.deepStrictEqual(findSelection(content, capture), {
  start,
  end: start + selected.length,
});
assert.strictEqual(blockEndOffset(content, capture.endOffset), content.indexOf("\n\n"));

const moved = `前缀\n\n${content}`;
assert.deepStrictEqual(findSelection(moved, capture), {
  start: moved.indexOf(selected),
  end: moved.indexOf(selected) + selected.length,
});
assert.strictEqual(
  findSelection(`${content}\n${selected}`, {
    ...capture,
    startOffset: -1,
    endOffset: -1,
  }),
  null
);
const whitespaceCapture = {
  selection: "第一段第一行 第一段第二行",
  startOffset: -1,
  endOffset: -1,
};
assert.deepStrictEqual(findSelection(content, whitespaceCapture), {
  start: 0,
  end: "第一段第一行\n第一段第二行".length,
});

const anchored = "第一段\n^nh-existing\n\n第二段";
assert.strictEqual(
  existingBlockId(anchored, 0, anchored.indexOf("\n\n")),
  "nh-existing"
);

const pageElement = {
  getAttribute: (name) => (name === "data-page-number" ? "12" : null),
};
pageElement.closest = () => pageElement;
const textNode = { nodeType: 3, parentElement: pageElement };
assert.strictEqual(
  pageNumberFromSelection({
    rangeCount: 1,
    isCollapsed: false,
    anchorNode: textNode,
    focusNode: textNode,
  }),
  12
);
assert.strictEqual(
  pageNumberFromSelection({
    rangeCount: 0,
    isCollapsed: true,
    anchorNode: null,
    focusNode: null,
  }),
  null
);

function mockLink(classNames, href, label) {
  return {
    classList: { contains: (name) => classNames.includes(name) },
    getAttribute: (name) => (name === "data-href" ? href : null),
    textContent: label,
  };
}

assert.deepStrictEqual(
  originalLinkInfo(mockLink(["internal-link"], "Source.md#^nh-abc", "来源：Source.md")),
  { href: "Source.md#^nh-abc", label: "来源：Source.md" }
);
assert.strictEqual(
  originalLinkInfo(mockLink(["internal-link"], "Other.md", "普通链接")),
  null
);
assert.strictEqual(
  originalLinkInfo(mockLink(["external-link"], "Source.md#^nh-abc", "来源")),
  null
);

const sourceModeLink = {
  classList: { contains: (name) => name === "cm-hmd-internal-link" },
  getAttribute: () => null,
  textContent: "正文库/问题.md#^nh-body-source-abc|原文",
  closest: (selector) => selector === ".cm-line"
    ? { textContent: "- [[正文库/问题.md#^nh-body-source-abc|原文]]" }
    : sourceModeLink,
};
assert.deepStrictEqual(
  originalLinkInfo(sourceModeLink),
  { href: "正文库/问题.md#^nh-body-source-abc", label: "原文" }
);

const sourceModeLine = {
  classList: { contains: (name) => name === "cm-line" },
  getAttribute: () => null,
  textContent: "- [[文档资料库/资料.pdf#page=10|来源：资料.pdf]]",
  closest: (selector) => selector === ".cm-line" ? sourceModeLine : null,
};
assert.deepStrictEqual(
  parseWikiLinkAtOffset(sourceModeLine.textContent, 8),
  { href: "文档资料库/资料.pdf#page=10", label: "来源：资料.pdf" }
);
assert.strictEqual(
  linkElementFromEvent({
    target: {},
    composedPath: () => [sourceModeLine, { classList: { contains: () => false } }],
  }),
  sourceModeLine
);

assert.strictEqual(
  isPlainLeftClick({ button: 0, ctrlKey: false, altKey: false, shiftKey: false, metaKey: false }),
  true
);
assert.strictEqual(
  isPlainLeftClick({ button: 0, ctrlKey: true, altKey: false, shiftKey: false, metaKey: false }),
  false
);
assert.strictEqual(
  isPlainLeftClick({ button: 0, ctrlKey: false, altKey: true, shiftKey: false, metaKey: false }),
  false
);

// ---------------------------------------------------------------- 高光
// 颜色一律归一成**名字**。踩过一次：渲染是按 ::highlight(nh-hl-yellow) 注册的，
// 而桌面端发的是十六进制色值，直接拼名字会得到 nh-hl-#e0ac00 —— 没有对应 CSS
// 规则，**画不出来还不报错**，PDF 与 Markdown 同时无色就是这么来的。
assert.strictEqual(normalizeHighlightColor("yellow"), "yellow");
assert.strictEqual(normalizeHighlightColor("BLUE"), "blue");
assert.strictEqual(normalizeHighlightColor("#e0ac00"), "yellow", "十六进制要映射回名字");
assert.strictEqual(normalizeHighlightColor("#E0AC00"), "yellow");
assert.strictEqual(normalizeHighlightColor("#08b94e"), "green");
assert.strictEqual(normalizeHighlightColor("#086ddd"), "blue");
assert.strictEqual(normalizeHighlightColor(""), "");
assert.strictEqual(normalizeHighlightColor("javascript:alert(1)"), "");
const css = require("fs").readFileSync(path.join(
  __dirname, "..", "obsidian-plugin", "noting-helper-bridge", "styles.css",
), "utf8");
for (const color of ["yellow", "green", "blue"]) {
  assert.ok(css.includes(`.cm-content .nh-hl-${color}`),
    `编辑器 ${color} 装饰应有对应的 CSS 背景`);
}

assert.strictEqual(normalizeHighlightColor("#12345"), "");
// 渲染期再归一一次：Vault 里已经存进去的十六进制锚点也要能画出来，不迁数据
assert.strictEqual(renderedColor("#e0ac00"), "yellow");
assert.strictEqual(renderedColor("green"), "green");
assert.strictEqual(renderedColor("乱七八糟"), "yellow", "认不出来时给个能画的默认值");

// ---- 卡片上的两个时间：格式按「今天 / 今年 / 更早」三档退，取不到就整块不显示 ----
const pad = (value) => String(value).padStart(2, "0");
const clockOf = (date) => `${pad(date.getHours())}:${pad(date.getMinutes())}`;
const now = new Date();
// 三档都要有断言，否则「今天只给时刻」这类退化会在跨年时才被发现。
assert.strictEqual(highlightTimeLabel(now.getTime()), clockOf(now));
const thisYear = new Date(now.getFullYear(), 0, 2, 3, 4);
assert.strictEqual(highlightTimeLabel(thisYear.getTime()),
  `01-02 ${clockOf(thisYear)}`);
const older = new Date(now.getFullYear() - 3, 11, 31, 23, 59);
assert.strictEqual(highlightTimeLabel(older.getTime()),
  `${older.getFullYear()}-12-31 23:59`);
// 缺字段是常态（旧记录没有 updatedAt），必须安静地返回空串而不是 1970 或 NaN。
for (const missing of [undefined, null, 0, -1, "昨天", NaN, {}]) {
  assert.strictEqual(highlightTimeLabel(missing), "",
    `${String(missing)} 不应被当成有效时间`);
}
// 「修改注释」只认注释类字段：改颜色、编辑器造成的原文漂移都不算，
// 否则用户每敲一个字都会刷新 changed_time。
assert.strictEqual(touchesAnnotation({ note: "x" }), true);
assert.strictEqual(touchesAnnotation({ noteLayout: "x" }), true);
assert.strictEqual(touchesAnnotation({ images: [] }), true);
assert.strictEqual(touchesAnnotation({ color: "blue" }), false);
assert.strictEqual(touchesAnnotation({ start: 3, end: 9 }), false);
assert.strictEqual(touchesAnnotation(null), false);

// ---- 划词定位：重复片段要用「离原位置最近」来定，而不是直接拒绝 ----
// 用户实测：他的索引文档里同一段原文被多个条目重复引用，于是正文的选区一选
// 就有好几处候选。这里原先直接拒绝——**手上有编辑器给的精确偏移却不用**。
// 标题之所以几乎都能成，正因为标题是唯一的。
const dupDoc = "甲处引用了「重点句」。乙处又引用了「重点句」。";
const dupSecond = dupDoc.lastIndexOf("重点句");
const dupFirst = dupDoc.indexOf("重点句");
assert.ok(dupFirst >= 0 && dupSecond > dupFirst);
// 偏移可用 → 挑离它最近的那一处（不是「第一个」）
assert.deepStrictEqual(
  findSelection(dupDoc, {
    selection: "重点句", startOffset: dupSecond, endOffset: dupSecond + 3,
  }),
  { start: dupSecond, end: dupSecond + 3 },
);
assert.deepStrictEqual(
  findSelection(dupDoc, {
    selection: "重点句", startOffset: dupFirst, endOffset: dupFirst + 3,
  }),
  { start: dupFirst, end: dupFirst + 3 },
);
// 偏移不可用（PDF 那条路就是 -1）→ 仍然不猜
assert.strictEqual(
  findSelection(dupDoc, { selection: "重点句", startOffset: -1, endOffset: -1 }),
  null,
);
// 两处一样近 → 不猜。偏移要落在两处**正中间**才算真的一样近
// （落在某一处上时那处胜出，是正确行为，不是歧义）
const tieDoc = "重点句123456789重点句";
assert.strictEqual(tieDoc.lastIndexOf("重点句"), 12);
assert.strictEqual(
  findSelection(tieDoc, { selection: "重点句", startOffset: 6, endOffset: 9 }),
  null,
);

// 阅读模式没有可靠的 editor 选区，且隐藏编辑器可能保留旧选区。
// 只能从当前 Markdown 视图的 DOM 取，不能误用别的分区的选区。
const previousSelection = globalThis.getSelection;
try {
  const inside = {}, outside = {};
  const preview = {
    getMode: () => "preview",
    editor: { getSelection: () => "旧选区" },
    containerEl: { contains: (node) => node === inside },
  };
  globalThis.getSelection = () => ({
    anchorNode: inside, focusNode: inside, toString: () => "阅读模式的新选区",
  });
  assert.deepStrictEqual(markdownViewSelection(preview), {
    selection: "阅读模式的新选区", startOffset: -1, endOffset: -1,
  });
  globalThis.getSelection = () => ({
    anchorNode: outside, focusNode: outside, toString: () => "另一分区的文字",
  });
  assert.strictEqual(markdownViewSelection(preview).selection, "");
  const edit = {
    getMode: () => "source",
    editor: {
      getSelection: () => "编辑器选区",
      getCursor: (side) => side === "from" ? 10 : 15,
      posToOffset: (position) => position,
    },
  };
  assert.deepStrictEqual(markdownViewSelection(edit), {
    selection: "编辑器选区", startOffset: 10, endOffset: 15,
  });
} finally {
  globalThis.getSelection = previousSelection;
}

// ---- 文本锚点：位置信息住在正文之外，靠「原文 + 前后文」重定位 ----

// 新锚点取原文与前后最多各 64 字符，旧的 32 字锚点仍可解析。
const quoteDoc = "前".repeat(80) + "被划选的文本" + "后".repeat(80);
const quote = describeQuote(quoteDoc, 80, 86);
assert.strictEqual(quote.exact, "被划选的文本");
assert.strictEqual(quote.prefix.length, 64);
assert.strictEqual(quote.suffix.length, 64);
// 贴着文档开头/结尾时上下文自然更短，不能因此崩
const headQuote = describeQuote("开头就是重点", 0, 3);
assert.strictEqual(headQuote.prefix, "");
assert.strictEqual(headQuote.exact, "开头就是重点".slice(0, 3));
assert.strictEqual(resolveAnchor("开头就是重点", headQuote).start, 0);

// 归一化：中文笔记里全角空格、全角破折号、中文引号很常见，不折叠就会
// 「明明没改却匹配不上」。折叠出来的字符要能映回源文本那一段。
const foldedSource = "甲　乙——丙“丁”";
const folded = normalizeWithMap(foldedSource);
assert.strictEqual(folded.text, "甲 乙--丙\"丁\"");
assert.strictEqual(folded.index.length, folded.text.length, "归一化位置必须逐个可映回");
assert.strictEqual(folded.index[0], 0);
assert.strictEqual(foldedSource[folded.index[1]], "　", "折叠出的空格要映回源里的全角空格");
// 一个源字符折成多个字符时，这几个输出位置都要映回**同一个**源下标——
// 这是「归一化位置 -> 源位置」映射最容易写错的地方
const ellipsis = normalizeWithMap("等等…");
assert.strictEqual(ellipsis.text, "等等...");
assert.strictEqual(ellipsis.index.length, 5);
assert.deepStrictEqual(ellipsis.index.slice(2), [2, 2, 2]);

// 重定位阶梯
const live = "第一段。\n\n这里是重点内容。\n\n第三段。";
const at = live.indexOf("重点内容");
const anchor = { ...describeQuote(live, at, at + 4), start: at, end: at + 4 };

// 1) 上下文完全吻合
assert.deepStrictEqual(resolveAnchor(live, anchor), {
  start: at, end: at + 4, method: "exact-with-context",
});
// 2) 文档被改动、偏移已腐烂，但上下文仍在 → 仍然认得出来
const shifted = "新加的一整段。\n\n" + live;
assert.strictEqual(resolveAnchor(shifted, anchor).method, "exact-with-context");
assert.strictEqual(resolveAnchor(shifted, anchor).start, at + "新加的一整段。\n\n".length);
// 3) 只有原文能对上（上下文也被改掉了）→ 退到 exact
const noContext = resolveAnchor(live, { exact: "重点内容" });
assert.deepStrictEqual(noContext, { start: at, end: at + 4, method: "exact" });
// 4) 只有归一化之后才一致 → 退到归一化那一级。
//    这正是中文笔记里的典型情形：原稿用的是全角空格，后来被换成普通空格。
//    逐字比对会失败（明明没改内容却匹配不上），归一化之后就对上了。
const fullWidth = "这里是重点。\n\n这里是　重点内容。\n\n结束。";
const atFw = fullWidth.indexOf("这里是　重点内容。");
const fwAnchor = { ...describeQuote(fullWidth, atFw, atFw + 9), start: atFw, end: atFw + 9 };
assert.strictEqual(fwAnchor.prefix, "这里是重点。\n\n");
const spacedDoc = fullWidth.replace("这里是　重点内容。", "这里是 重点内容。");
const normalizedHit = resolveAnchor(spacedDoc, fwAnchor);
assert.ok(normalizedHit, "归一化之后应当能对上");
assert.strictEqual(normalizedHit.method, "normalized-with-context");
assert.strictEqual(spacedDoc.slice(normalizedHit.start, normalizedHit.end), "这里是 重点内容。");
// 5) 同一片段出现多次、上下文也区分不了 → **不猜**，返回 null
const repeated = "重复句。\n\n中间。\n\n重复句。";
assert.strictEqual(resolveAnchor(repeated, { exact: "重复句。" }), null);
// 但上下文能区分时就要认出来——这是 32 字符上下文存在的意义
const twins = "甲区。这里的重点句。\n\n乙区。这里的重点句。";
const second = twins.indexOf("这里的重点句。", 10);
const twinAnchor = { ...describeQuote(twins, second, second + 6) };
assert.strictEqual(resolveAnchor(twins, twinAnchor).start, second);

// 两处选区贴身的 32 字上下文完全相同，但第 33~64 字不同：旧锚点
// 确实定不下来，新锚点应靠长上下文选中第二处（而非偏移或第一处）。
const longTwins = "甲".repeat(40) + "同".repeat(40) + "目标" + "同".repeat(40)
  + "乙".repeat(40) + "同".repeat(40) + "目标" + "同".repeat(40);
const longSecond = longTwins.lastIndexOf("目标");
const shortQuote = describeQuote(longTwins, longSecond, longSecond + 2, 32);
const longQuote = describeQuote(longTwins, longSecond, longSecond + 2);
assert.strictEqual(resolveAnchor(longTwins, shortQuote), null);
assert.deepStrictEqual(resolveAnchor(longTwins, longQuote), {
  start: longSecond, end: longSecond + 2, method: "exact-with-context",
});

// 外围 64 字里的字符变化了，但贴身 32 字仍唯一时应回退，不能让新锚点
// 比旧锚点更脆弱。原文重复，故这并不是单靠 exact 就能通过的测试。
const fallbackDoc = "甲".repeat(64) + "目标" + "同".repeat(64)
  + "乙".repeat(64) + "目标" + "同".repeat(64);
const fallbackSecond = fallbackDoc.lastIndexOf("目标");
const fallbackQuote = describeQuote(fallbackDoc, fallbackSecond, fallbackSecond + 2);
const legacyQuote = describeQuote(fallbackDoc, fallbackSecond, fallbackSecond + 2, 32);
assert.strictEqual(resolveAnchor(fallbackDoc, legacyQuote).start, fallbackSecond,
  "旧版 32 字锚点仍然可读");
const editedDoc = fallbackDoc.slice(0, fallbackSecond - 64)
  + "丙".repeat(16) + fallbackDoc.slice(fallbackSecond - 48);
assert.deepStrictEqual(resolveAnchor(editedDoc, fallbackQuote), {
  start: fallbackSecond, end: fallbackSecond + 2, method: "exact-with-context",
});
// 编辑器跨行时 DOM 不带换行：较远上下文被改且近处有换行，
// 仍要能在 loose 阶梯里用 32 字上下文唯一定位。
const editedCrossLine = editedDoc.slice(0, fallbackSecond) + "\n"
  + editedDoc.slice(fallbackSecond);
assert.deepStrictEqual(resolveAnchor(editedCrossLine, fallbackQuote), {
  start: fallbackSecond + 1, end: fallbackSecond + 3, method: "loose-with-context",
});
// 两处 64 字上下文也完全相同 → 不猜；旧 32 字锚点照常读。
const indistinguishable = ("甲".repeat(70) + "目标" + "乙".repeat(70)).repeat(2);
const sameSecond = indistinguishable.lastIndexOf("目标");
assert.strictEqual(resolveAnchor(indistinguishable,
  describeQuote(indistinguishable, sameSecond, sameSecond + 2)), null);
// 文首另有 exact+suffix 不代表原 prefix 可丢弃；此时两处原文歧义，拒绝。
const misplaced = describeQuote("前缀目标后缀", 2, 4);
assert.strictEqual(resolveAnchor("目标后缀……目标别处", misplaced), null);

// 浮层渲染（阅读视图 / 编辑视图）走的是**同一套阶梯**，不是只比 exact。
// 只比 exact 是过度保守：用户实测撞上过——他在索引文档里划词，而索引条目本身
// 会重复引用同一段文字，于是「出现 2 次」的锚点整条被跳过、画不出来。
const repeatedDom = "甲：重点句。\n\n乙：重点句。";
assert.strictEqual(
  resolveAnchor(repeatedDom, { exact: "重点句。" }), null,
  "没有上下文时确实定不下来，不猜",
);
const domSecond = repeatedDom.indexOf("重点句。", 3);
const domAnchor = { ...describeQuote(repeatedDom, domSecond, domSecond + 4) };
assert.notStrictEqual(domSecond, -1);
assert.strictEqual(
  resolveAnchor(repeatedDom, domAnchor).method, "exact-with-context",
  "有上下文就该认出来，不该因为 exact 重复而放弃",
);

// 索引追加了相同原文后，编辑器虚拟化可能只渲染新记录的副本。
// 原先的 exact-only 兜底会把颜色画到副本上；源文件重复时必须要上下文。
const original = "前".repeat(70) + "重点句子" + "后".repeat(70);
const source = original + "\n\n## 记录\n- 原文：重点句子";
const originalAt = original.indexOf("重点句子");
const indexAnchor = describeQuote(original, originalAt, originalAt + 4);
const onlyRecordVisible = "## 记录\n- 原文：重点句子";
assert.strictEqual(sourceHasRepeatedExact(source, indexAnchor), true);
assert.strictEqual(resolveAnchor(onlyRecordVisible, indexAnchor).method, "exact",
  "这重现了旧逻辑会错画在记录副本上的原因");
assert.strictEqual(resolveAnchor(onlyRecordVisible, indexAnchor,
  { requireContext: true }), null, "原选区不在可见 DOM 时不能画到副本上");
assert.strictEqual(resolveAnchor(original, indexAnchor,
  { requireContext: true }).method, "exact-with-context");
assert.strictEqual(sourceHasRepeatedExact(original, indexAnchor), false,
  "其他普通 Markdown 的唯一原文保持既有兜底");

// 编辑视图直接按源文档坐标绘制，完全不受可见 DOM / 重复内容影响。
const rootHost = Object.create(PluginClass.prototype);
rootHost.app = { workspace: { getLeavesOfType() { return [{ view: {
  file: { path: "索引.md" }, editor: { cm: { state: { doc: { toString: () => source } } } },
} }]; } } };
assert.strictEqual(rootHost.markdownContentRoots().length, 1);
assert.deepStrictEqual(editorAnchorSpan(source, { kind: "md", ...indexAnchor }),
  { from: originalAt, to: originalAt + 4 });
assert.deepStrictEqual(editorMarksForDocument(source, [{
  kind: "md", ...indexAnchor, color: "green",
}])[0], { from: originalAt, to: originalAt + 4, color: "green" });
assert.deepStrictEqual(editorAnchorSpan(source, {
  kind: "md", exact: "重点句子", start: source.lastIndexOf("重点句子"),
}), { from: source.lastIndexOf("重点句子"), to: source.length });
const repeatedFive = ["同样内容", "同样内容", "同样内容", "同样内容", "同样内容"].join("\n");
const fiveAnchors = Array.from({ length: 5 }, (_, i) => ({
  kind: "md", exact: "同样内容", start: i * 5, color: "yellow",
}));
assert.deepStrictEqual(editorMarksForDocument(repeatedFive, fiveAnchors)
  .map(({ from, to }) => [from, to]),
[[0, 4], [5, 9], [10, 14], [15, 19], [20, 24]],
"同一原文重复五次，每一处都应画在用户选中的偏移，不取可见 DOM 首项");
// 成功后只收起仍然相同的原生选区；不动用户等待期间新选中的内容。
const selectedNode = {};
const sampleSelection = { rangeCount: 1, isCollapsed: false,
  anchorNode: selectedNode, focusNode: selectedNode,
  toString: () => "PDF 片段", removeAllRanges() { this.cleared = true; },
};
const pdfSurface = { ownerDocument: { getSelection: () => sampleSelection },
  contains: (node) => node === selectedNode };
assert.strictEqual(clearDomSelectionInside(pdfSurface, "别的文字"), false);
assert.strictEqual(sampleSelection.cleared, undefined);
assert.strictEqual(clearDomSelectionInside(pdfSurface, "PDF 片段"), true);
assert.strictEqual(sampleSelection.cleared, true);

// CM6 状态字段在用户编辑时自动 map，且连续追加精确落在不同副本。
let cmState = EditorState.create({ doc: "重复 重复 重复 more", extensions: [editorMarks] });
cmState = cmState.update({ effects: replaceEditorMarks.of([
  { from: 3, to: 5, color: "yellow" },
  { from: 6, to: 8, color: "green" },
]) }).state;
assert.deepStrictEqual(cmRanges(cmState), [[3, 5], [6, 8]]);
cmState = cmState.update({ changes: { from: 0, insert: "前" } }).state;
assert.deepStrictEqual(cmRanges(cmState), [[4, 6], [7, 9]]);
cmState = cmState.update({ effects: appendEditorMark.of({
  from: 10, to: 12, color: "blue",
}) }).state;
assert.deepStrictEqual(cmRanges(cmState), [[4, 6], [7, 9], [10, 12]]);
// 新增必须在原 RangeSet 上增量更新，不能扫描+排序重建所有旧装饰。
const fieldSource = require("fs").readFileSync(path.join(__dirname,
  "..", "obsidian-plugin", "noting-helper-bridge", "source.js"), "utf8");
const appendBranch = fieldSource.slice(fieldSource.indexOf("if (effect.is(appendEditorMark))"),
  fieldSource.indexOf("return mapped;", fieldSource.indexOf("if (effect.is(appendEditorMark))")));
assert.ok(appendBranch.includes("mapped.update({ add:"));
assert.ok(!appendBranch.includes("marksFromSet(mapped)"));
let collapseAt = null;
let selectedText = "重复";
const markAnchor = { kind: "md", exact: "重复", start: 4, end: 6, color: "yellow" };
const selectionView = { editor: {
  cm: { state: cmState },
  getSelection: () => selectedText,
  getCursor: (which) => ({ offset: which === "from" ? 4 : 6 }),
  posToOffset: (position) => position.offset,
  offsetToPos: (offset) => ({ offset }),
  setSelection: (from, to) => { collapseAt = [from.offset, to.offset]; },
} };
assert.strictEqual(clearEditorSelectionIfPainted(selectionView, markAnchor), true);
assert.deepStrictEqual(collapseAt, [6, 6]);
collapseAt = null;
selectedText = "新划的文字";
assert.strictEqual(clearEditorSelectionIfPainted(selectionView, markAnchor), false);
assert.strictEqual(collapseAt, null);

function cmRanges(state) {
  const result = [];
  state.field(editorMarks).between(0, state.doc.length, (from, to) => result.push([from, to]));
  return result;
}


// 渲染层与源文本最大的**系统性**差异：源文件里有换行，编辑器 DOM 里相邻行
// 是黏在一起的（CodeMirror 每行一个 div，textContent 拼起来不插分隔符）。
// 只「收缩空白」抹不平——一边有一个空格、另一边什么都没有。
// 实测 93 个取样位置：加这一级把渲染成功率从 51% 提到 78%。
const NL = String.fromCharCode(10);        // 不写字面量，免得被工具链转义成真换行
const lineSource = `第一行内容${NL}第二行内容${NL}第三行内容`;
const lineDom = "第一行内容第二行内容第三行内容";
const crossAnchor = { ...describeQuote(lineSource, 3, 12) };
assert.ok(crossAnchor.exact.includes(NL), "这个锚点确实是跨行的");
assert.strictEqual(
  resolveAnchor(lineSource, crossAnchor).method, "exact-with-context",
  "原样存在时必须走最可信的那一级",
);
const crossHit = resolveAnchor(lineDom, crossAnchor);
assert.ok(crossHit, "DOM 里没有那个换行，也应当能对上");
assert.strictEqual(crossHit.method, "loose-with-context");
assert.strictEqual(lineDom.slice(crossHit.start, crossHit.end), "内容第二行内容");
// 只比 exact 的那一级也一样：换行差异不该让它彻底失配
assert.ok(resolveAnchor(lineDom, { exact: `内容${NL}第二行内容` }));

// 视口二分：只取落在可视区内的（装饰只画视口内，开销与总高光数基本无关）
const sorted = [
  { start: 0, end: 5 }, { start: 10, end: 15 },
  { start: 20, end: 25 }, { start: 30, end: 35 },
];
assert.deepStrictEqual(anchorsInRange(sorted, 12, 22).map((a) => a.start), [10, 20]);
assert.deepStrictEqual(anchorsInRange(sorted, 100, 200), []);
assert.deepStrictEqual(anchorsInRange(sorted, 0, 5).map((a) => a.start), [0]);

// ---- PDF 页内定位：一页的文字散在多个 span 里，短语常横跨好几个 ----
const spans = [
  { textContent: "RTRL 算法和 " },
  { textContent: "BPTT 算法都是" },
  { textContent: "基于梯度下降的算法。" },
];
const page = readPageText(spans);
assert.strictEqual(page.text, "RTRL 算法和 BPTT 算法都是基于梯度下降的算法。");
assert.strictEqual(page.parts.length, 3);

// 短语横跨两个 span——这是 PDF 里最常见的情形（PDF.js 按排版切 span）
const hit = locateInPage(page.text, "BPTT 算法都是基于");
assert.strictEqual(page.text.slice(hit.start, hit.end), "BPTT 算法都是基于");
const slices = slicesForRange(page.parts, hit.start, hit.end);
assert.strictEqual(slices.length, 2, "跨两个 span 的短语应当映射出两段");
// 按性质断言，而不是手数下标：各段拼起来必须正好是定位到的那串文字，
// 且每一段都落在它自己的 span 长度之内
assert.strictEqual(
  slices.map((s) => (s.node.textContent || "").slice(s.from, s.to)).join(""),
  "BPTT 算法都是基于",
);
for (const slice of slices) {
  assert.ok(slice.from >= 0 && slice.to <= slice.node.textContent.length);
  assert.ok(slice.to > slice.from);
}
assert.strictEqual(slices[0].node, spans[1]);
assert.strictEqual(slices[1].node, spans[2]);

// PDF.js 的选区调 toString() 时会在行与行之间插换行，而文本层里各 span 的
// textContent 直接拼起来没有那些分隔。不归一化，**跨行选区永远匹配不到**——
// 表现为「明明选的就是这页的字，却一段都画不出来」。这条是实测后补上的。
const twoLines = readPageText([
  { textContent: "记忆容量（Memory" },
  { textContent: " Capacity）是指" },
]);
assert.strictEqual(twoLines.text, "记忆容量（Memory Capacity）是指");
assert.deepStrictEqual(
  locateInPage(twoLines.text, "记忆容量（Memory"), { start: 0, end: 11 },
);
const crossLine = locateInPage(twoLines.text, "容量（Memory\nCapacity）是指");
assert.ok(crossLine, "跨行选区（toString 带换行）应当能对上");
assert.strictEqual(
  twoLines.text.slice(crossLine.start, crossLine.end), "容量（Memory Capacity）是指",
);
// 全角空格换成普通空格也要能对上
assert.ok(locateInPage(twoLines.text, "容量（Memory　Capacity）是指"));

// PDF.js 视觉换行不保证在相邻 span 的文本中留空格。以前只有
// 「换行 ↔ 空格」用例，中文/英文连续拼接时捕获直接返回 -1。
const joinedLines = readPageText([
  { textContent: "这是第一行" }, { textContent: "这是第二行" },
  { textContent: "这是第三行" },
]);
const multiLineExact = "第一行\n这是第二行\n这是第三行";
const multiLineHit = locateInPage(joinedLines.text, multiLineExact);
assert.ok(multiLineHit, "三行选区即使文本层零空格拼接也应定位");
assert.strictEqual(joinedLines.text.slice(multiLineHit.start, multiLineHit.end),
  "第一行这是第二行这是第三行");
assert.strictEqual(occurrenceForOffset(joinedLines.text, multiLineExact,
  joinedLines.text.indexOf("第一行")), 0, "捕获阶段也必须使用同一归一化候选");
const repeatedLines = "甲行乙行。甲行乙行。";
const repeatedExact = "甲行\n乙行";
const repeatedSecond = repeatedLines.indexOf("甲行", 1);
assert.strictEqual(occurrenceForOffset(repeatedLines, repeatedExact, repeatedSecond), 1);
assert.deepStrictEqual(locateInPage(repeatedLines, repeatedExact, 1),
  { start: repeatedSecond, end: repeatedSecond + "甲行乙行".length });
assert.strictEqual(locateInPage(repeatedLines, "不匹配\n任何片段"), null);

// 同一页里出现多次时按 occurrence 取对
const twice = "重点在甲处。中间。重点在乙处。";
const secondAt = twice.indexOf("重点", 1);
assert.notStrictEqual(secondAt, -1);
assert.deepStrictEqual(locateInPage(twice, "重点", 0), { start: 0, end: 2 });
assert.deepStrictEqual(
  locateInPage(twice, "重点", 1), { start: secondAt, end: secondAt + 2 },
);
// occurrence 越界时夹到最后一处，不返回 null
assert.deepStrictEqual(
  locateInPage(twice, "重点", 9), { start: secondAt, end: secondAt + 2 },
);
assert.strictEqual(locateInPage(twice, "不存在的词", 0), null);

// 用「选区起点落在哪一处」判定用户选的是第几个
assert.strictEqual(occurrenceForOffset(twice, "重点", 0), 0);
assert.strictEqual(occurrenceForOffset(twice, "重点", secondAt), 1);
assert.strictEqual(occurrenceForOffset(twice, "重点", secondAt + 1), 1);
assert.strictEqual(occurrenceForOffset(twice, "重点", -1), 0, "认不出端点时退回第一个");
assert.strictEqual(occurrenceForOffset(twice, "不存在的词", 3), -1);

// ---- 锚点存储：读不出来不能把插件带走 ----

function fakeStoreHost(initial, shared) {
  const state = shared || { written: null, files: {} };
  const legacy = ".obsidian/plugins/noting-helper-bridge/highlights.json";
  if (initial !== undefined && !shared && initial !== null) state.files[legacy] = initial;
  return {
    state,
    manifest: { dir: ".obsidian/plugins/noting-helper-bridge" },
    settings: {highlightJournal:"NoteDataOfY/高光库/高光数据.jsonl"},
    fixedHighlightJournal: `${legacy.slice(0, -"highlights.json".length)}storage/highlights.jsonl`,
    saveData: async () => {},
    replayHighlightLines: PluginClass.prototype.replayHighlightLines,
    rebuildHighlightCache: PluginClass.prototype.rebuildHighlightCache,
    syncOpenAnnotationCache: PluginClass.prototype.syncOpenAnnotationCache,
    updateOpenAnnotation: PluginClass.prototype.updateOpenAnnotation,
    app: { vault: { adapter: {
      exists: async (file) => file in state.files || Object.keys(state.files).some((p) => p.startsWith(`${file}/`)),
      read: async (file) => {
        if (!(file in state.files)) throw new Error("ENOENT");
        return state.files[file];
      },
      list: async (dir) => ({ files: Object.keys(state.files)
        .filter((file) => file.startsWith(`${dir}/`)) }),
      write: async (file, data) => {
        state.written = { path: file, data };
        state.files[file] = data;
      },
    } } },
    highlightStorePath: PluginClass.prototype.highlightStorePath,
    highlightJournalDir: PluginClass.prototype.highlightJournalDir,
    highlightJournalName: PluginClass.prototype.highlightJournalName,
    migrateHighlightStorage: PluginClass.prototype.migrateHighlightStorage,
    writeSnapshot: async (file, data) => { state.files[file] = data; },
    flushAnchorEdits: async () => {},
    checkedStoragePath: PluginClass.prototype.checkedStoragePath,
    storagePaths: PluginClass.prototype.storagePaths,
    highlightImageDirectory: PluginClass.prototype.highlightImageDirectory,
    appendAnchorRecord: PluginClass.prototype.appendAnchorRecord,
    appendHighlightRecord: PluginClass.prototype.appendHighlightRecord,
    loadHighlights: PluginClass.prototype.loadHighlights,
    anchorsForFile: PluginClass.prototype.anchorsForFile,
    addAnchor: PluginClass.prototype.addAnchor,
    findOverlappingAnchor: PluginClass.prototype.findOverlappingAnchor,
    highlightStore: null,
    refreshes: 0,
    pushEditorAnchor() {},
    scheduleHighlightRefresh() { this.refreshes += 1; },
  };
}

(async () => {
  // 文件不存在 → 空库起步，不抛
  const empty = fakeStoreHost(null);
  assert.deepStrictEqual(await empty.loadHighlights(), { version: 2, files: {} });
  assert.deepStrictEqual(empty.anchorsForFile("a.md"), []);

  // 损坏的旧库不能迁移成空的私有主库。
  const broken = fakeStoreHost("{ 这不是 JSON");
  await assert.rejects(() => broken.loadHighlights(), /旧高光迁移中止/);
  assert.ok(!(broken.fixedHighlightJournal in broken.state.files));
  const weird = fakeStoreHost(JSON.stringify({ version: 1, files: "oops" }));
  await assert.rejects(() => weird.loadHighlights(), /旧库结构无效/);
  const missing = fakeStoreHost(null);
  missing.settings.highlightJournal = missing.fixedHighlightJournal;
  await assert.rejects(() => missing.loadHighlights(), /日志丢失/,
    '固定主库丢失时不能重新导入旧测试数据');

  // 正常往返：写进的是插件目录，不是一个随机路径
  const live = fakeStoreHost(JSON.stringify({ version: 2, files: {} }));
  await live.addAnchor("课程/第一章.md", {
    kind: "md", start: 3, end: 6, exact: "重点", prefix: "", suffix: "",
    color: "yellow", id: "hl-1",
  });
  assert.strictEqual(
    live.state.written.path,
    live.settings.highlightJournal,
  );
  assert.strictEqual(live.anchorsForFile("课程/第一章.md").length, 1);
  assert.strictEqual(live.refreshes, 0, "Markdown 编辑视图新增由 CodeMirror 增量装饰直接绘制");
  assert.ok(
    JSON.parse(live.state.written.data.trim().split("\n").at(-1)).anchor.exact === "重点",
  );
  // 插件重载：优先私有日志，旧 JSON 不再读入。
  const reloaded = fakeStoreHost(undefined, live.state);
  await reloaded.loadHighlights();
  assert.strictEqual(reloaded.anchorsForFile("课程/第一章.md").length, 1);
  const logPath = live.state.written.path;
  live.state.files[logPath] += "\n{partial\n";
  const journalRecovered = fakeStoreHost(undefined, live.state);
  await assert.rejects(() => journalRecovered.loadHighlights(), /日志损坏/,
    '私有日志不完整时拒绝写入，避免半截状态覆盖主库');
  live.state.files[logPath] = live.state.files[logPath].replace("\n{partial\n", "");

  // 重复检测：同一处文字不该被叠两条（改色是下一阶段的事）
  const dup = {
    kind: "md", start: 3, end: 6, exact: "重点", color: "green",
  };
  assert.ok(live.findOverlappingAnchor("课程/第一章.md", dup));
  assert.strictEqual(live.findOverlappingAnchor("课程/第一章.md", {
    kind: "md", start: 99, end: 102, exact: "别处", color: "green",
  }), null);
  // PDF 靠「页码 + 页内第几次出现」判重，不靠偏移
  const pdfHost = fakeStoreHost(null);
  await pdfHost.addAnchor("资料/讲义.pdf", {
    kind: "pdf", page: 12, exact: "重点", occurrence: 1, color: "blue", id: "hl-2",
  });
  assert.ok(pdfHost.findOverlappingAnchor("资料/讲义.pdf", {
    kind: "pdf", page: 12, exact: "重点", occurrence: 1,
  }));
  assert.strictEqual(pdfHost.findOverlappingAnchor("资料/讲义.pdf", {
    kind: "pdf", page: 12, exact: "重点", occurrence: 0,
  }), null);

  // 高光 POST 超时后首次写盘仍可能成功：同一个 capture_id 的请求必须只写一次。
  const file = Object.assign(new FakeTFile(), {
    path: "资料/讲义.pdf", name: "讲义.pdf", extension: "pdf",
  });
  const retryHost = Object.create(PluginClass.prototype);
  retryHost.settings = { token: "test-token" };
  retryHost.app = { vault: {
    getName: () => "测试仓库",
    getAbstractFileByPath: () => file,
  } };
  retryHost.captures = new Map([["capture-retry", {
    kind: "file", filePath: file.path, selection: "选中一句",
    pageNumber: 3, occurrence: 0, createdAt: Date.now(),
  }]]);
  retryHost.highlightStore = { version: 2, files: {} };
  retryHost.loadHighlights = async () => retryHost.highlightStore;
  retryHost.scheduleHighlightRefresh = () => {};
  retryHost.highlightInFlight = new Map();
  let finishSave;
  const saveWait = new Promise((resolve) => { finishSave = resolve; });
  let saveCount = 0;
  retryHost.appendAnchorRecord = async () => { saveCount += 1; await saveWait; };
  const response = () => ({
    status: null, body: null,
    writeHead(status) { this.status = status; },
    end(body) { this.body = JSON.parse(body); },
  });
  const request = () => Object.assign(
    Readable.from([JSON.stringify({ capture_id: "capture-retry", color: "yellow" })]),
    { method: "POST", url: "/highlight",
      headers: { authorization: "Bearer test-token" } },
  );
  const first = response(), second = response();
  const firstJob = retryHost.handleRequest(request(), first);
  const secondJob = retryHost.handleRequest(request(), second);
  await new Promise((resolve) => setImmediate(resolve));
  assert.strictEqual(saveCount, 1, "同一 capture 的并发重试不能同时写盘");
  finishSave();
  await Promise.all([firstJob, secondJob]);
  assert.strictEqual(first.status, 200);
  assert.strictEqual(second.status, 200);
  assert.strictEqual(first.body.highlight_id, second.body.highlight_id);
  assert.strictEqual(saveCount, 1);
  const saved = retryHost.anchorsForFile(file.path);
  assert.strictEqual(saved.length, 1);
  assert.strictEqual(saved[0].captureId, "capture-retry");
  assert.strictEqual(saved[0].colorHex, PluginClass._test.DEFAULT_HIGHLIGHT_PALETTE.yellow);
  // 颜色自 2.4.0 起固定成 8 种：旧 data.json 里残留的自定义调色板既不参与写入，
  // 也追改不了历史高光——colorHex 是创建当时的快照。
  retryHost.settings.highlightColors = { yellow: '#123456' };
  assert.strictEqual(saved[0].colorHex, PluginClass._test.DEFAULT_HIGHLIGHT_PALETTE.yellow,
    '退役的自定义调色板不追改历史高光');
  // 插件重新加载后只凭持久化锚点即可重放结果，不需要内存里的选区。
  const restarted = Object.create(PluginClass.prototype);
  restarted.app = retryHost.app;
  restarted.highlightStore = retryHost.highlightStore;
  restarted.captures = new Map();
  const replay = response();
  await restarted.applyHighlight({ capture_id: "capture-retry", color: "yellow" }, replay);
  assert.strictEqual(replay.status, 200);
  assert.strictEqual(replay.body.highlight_id, first.body.highlight_id);
  // Mode switches configure the native entry through an authenticated control route.
  const entryHost = Object.create(PluginClass.prototype);
  entryHost.settings = {token:'test-token',nativeHighlightEntry:false};
  let entrySaved=null;
  entryHost.saveData=async settings=>{entrySaved={...settings}};
  const entryRequest=(token,enabled)=>Object.assign(
    Readable.from([JSON.stringify({enabled})]),
    {method:'POST',url:'/highlight-entry',headers:{authorization:`Bearer ${token}`}});
  const forbiddenEntry=response();
  await entryHost.handleRequest(entryRequest('bad',true),forbiddenEntry);
  assert.strictEqual(forbiddenEntry.status,401);
  const entryOn=response();
  await entryHost.handleRequest(entryRequest('test-token',true),entryOn);
  assert.strictEqual(entryOn.status,200);
  assert.strictEqual(entryHost.settings.nativeHighlightEntry,true);
  assert.strictEqual(entrySaved.nativeHighlightEntry,true);
  const invalidEntry=response();
  await entryHost.handleRequest(entryRequest('test-token','yes'),invalidEntry);
  assert.strictEqual(invalidEntry.status,400);
  assert.strictEqual(entryHost.settings.nativeHighlightEntry,true);
  // 第一次请求等待写盘时客户端断开，也不能让异常影响后续重试。
  const disconnected = Object.create(PluginClass.prototype);
  disconnected.settings = { token: "test-token" };
  disconnected.app = retryHost.app;
  disconnected.captures = new Map([["capture-disconnected", {
    kind: "file", filePath: file.path, selection: "断线后仍写入",
    pageNumber: 4, occurrence: 0, createdAt: Date.now(),
  }]]);
  disconnected.highlightStore = { version: 2, files: {} };
  disconnected.loadHighlights = async () => disconnected.highlightStore;
  disconnected.scheduleHighlightRefresh = () => {};
  disconnected.highlightInFlight = new Map();
  let releaseDisconnected;
  const disconnectedWrite = new Promise((resolve) => { releaseDisconnected = resolve; });
  let disconnectedSaves = 0;
  disconnected.appendAnchorRecord = async () => {
    disconnectedSaves += 1;
    await disconnectedWrite;
  };
  const disconnectedRequest = () => Object.assign(
    Readable.from([JSON.stringify({ capture_id: "capture-disconnected", color: "blue" })]),
    { method: "POST", url: "/highlight",
      headers: { authorization: "Bearer test-token" } },
  );
  const dropped = { destroyed: true,
    writeHead() { throw new Error("closed socket"); }, end() {} };
  const recovered = response();
  const droppedJob = disconnected.handleRequest(disconnectedRequest(), dropped);
  const recoveredJob = disconnected.handleRequest(disconnectedRequest(), recovered);
  await new Promise((resolve) => setImmediate(resolve));
  releaseDisconnected();
  await Promise.all([droppedJob, recoveredJob]);
  assert.strictEqual(recovered.status, 200);
  assert.strictEqual(disconnectedSaves, 1);
  assert.strictEqual(disconnected.anchorsForFile(file.path).length, 1);

  // 写盘失败时内存也回滚，否则未成功的锚点会挡住重试。
  const failed = Object.create(PluginClass.prototype);
  failed.app = retryHost.app;
  failed.captures = new Map([["failed-capture", {
    kind: "file", filePath: file.path, selection: "另一句",
    pageNumber: 5, occurrence: 0, createdAt: Date.now(),
  }]]);
  failed.highlightStore = { version: 2, files: {} };
  failed.loadHighlights = async () => failed.highlightStore;
  failed.appendAnchorRecord = async () => { throw new Error("磁盘不可写"); };
  const failedResponse = response();
  await failed.applyHighlight({ capture_id: "failed-capture", color: "green" }, failedResponse);
  assert.strictEqual(failedResponse.status, 500);
  assert.deepStrictEqual(failed.anchorsForFile(file.path), []);

  // 持久化成功之前不绘制、不可交互；失败时原视图始终不变。
  const durable = Object.create(PluginClass.prototype);
  durable.highlightStore = { version: 2, files: {} };
  durable.loadHighlights = async () => durable.highlightStore;
  let releaseWrite, painted = 0, selectionCleared = false;
  durable.pushEditorAnchor = () => { painted += 1; };
  durable.scheduleHighlightRefresh = () => {};
  durable.appendAnchorRecord = async () => new Promise((resolve) => { releaseWrite = resolve; });
  const writeJob = durable.addAnchor('source.md', { id: 'durable', kind: 'md' },
    () => { selectionCleared = true; });
  await new Promise((resolve) => setImmediate(resolve));
  assert.strictEqual(painted, 0);
  assert.strictEqual(selectionCleared, false);
  assert.strictEqual(durable.anchorsForFile('source.md').length, 0);
  releaseWrite(); await writeJob;
  assert.strictEqual(painted, 1);
  assert.strictEqual(selectionCleared, true);
  assert.strictEqual(durable.anchorsForFile('source.md').length, 1);
  // 写盘回调一完成就同步注册视图，不依赖后台可能延迟的 80ms timer。
  const direct = Object.create(PluginClass.prototype);
  direct.highlightStore = { version: 2, files: {} };
  direct.loadHighlights = async () => direct.highlightStore;
  const order = [];
  direct.appendAnchorRecord = async () => { order.push('disk'); };
  direct.pushEditorAnchor = (_filePath, anchor) => { if (anchor.kind === 'md') order.push('editor'); };
  direct.refreshHighlights = () => { order.push('pdf/reading'); };
  direct.scheduleHighlightRefresh = () => { order.push('timer'); };
  const directTrace={};
  await direct.addAnchor('source.md', { id: 'direct', kind: 'md' },null,directTrace);
  assert.ok(directTrace.storage_commit_wall_ms<=directTrace.memory_updated_wall_ms);
  assert.ok(directTrace.memory_updated_wall_ms<=directTrace.annotation_cache_updated_wall_ms);
  assert.ok(directTrace.annotation_cache_updated_wall_ms<=directTrace.palette_ready_wall_ms);
  assert.strictEqual(directTrace.dom_refresh_needed,false);
  assert.deepStrictEqual(order, ['disk', 'editor']);
  assert.ok(!order.includes('timer'), 'MD 编辑视图新增无需重算 PDF Range');
  const pdfTrace={};
  await direct.addAnchor('source.pdf', { id:'direct-pdf', kind:'pdf' },null,pdfTrace);
  assert.strictEqual(pdfTrace.dom_refresh_needed,true);
  assert.ok(pdfTrace.dom_refresh_start_wall_ms<=pdfTrace.dom_refresh_end_wall_ms);
  assert.deepStrictEqual(order.slice(-2), ['disk', 'pdf/reading']);
  const failure = Object.create(PluginClass.prototype);
  failure.highlightStore = { version: 2, files: {} };
  failure.loadHighlights = async () => failure.highlightStore;
  failure.pushEditorAnchor = () => { painted += 1; };
  failure.scheduleHighlightRefresh = () => {};
  failure.appendAnchorRecord = async () => { throw new Error('disk full'); };
  await assert.rejects(failure.addAnchor('source.md', { id: 'failed', kind: 'md' }), /disk full/);
  assert.strictEqual(failure.anchorsForFile('source.md').length, 0);
  assert.strictEqual(painted, 1);

  // 探针只在显式请求时工作，下一帧标记与落盘时刻分别记录。
  const probeHost = Object.create(PluginClass.prototype);
  const probe = probeHost.armPaintProbe('trace-1');
  probeHost.signalPaintProbe('trace-1', 'markdown', () => true);
  const sampled = await probeHost.finishPaintProbe(probe);
  assert.strictEqual(sampled.visual_confirmed, true);
  assert.strictEqual(sampled.surface, 'markdown');
  assert.ok(sampled.visual_frame_wall_ms >= sampled.render_registered_wall_ms);

  // Windows 命名管道真实帧往返：同一鉴权路由，半包、错误码和成功响应。
  if (process.platform === 'win32') {
    const net = require('net');
    const pipeHost = Object.create(PluginClass.prototype);
    pipeHost.settings = {token: 'test-pipe-token-' + process.pid,
      port: 27000 + process.pid % 1000, highlightJournal: 'test.jsonl'};
    pipeHost.manifest = {version:'test'};
    pipeHost.app = {vault:{getName:()=> 'isolated-test'}};
    pipeHost.captures = new Map();
    pipeHost.highlightInFlight = new Map();
    let handled = 0;
    pipeHost.applyHighlight = async (_payload, reply) => {
      handled += 1;
      reply.writeHead(200); reply.end(JSON.stringify({ok:true,
        ...(_payload.trace ? { trace:_payload._pipeTrace,
          handler_wall_ms:_payload._receivedWallMs } : {})}));
    };
    pipeHost.startPipeServer();
    await new Promise((resolve,reject) => {
      if(pipeHost.pipeServer.listening) return resolve();
      pipeHost.pipeServer.once('listening',resolve);
      pipeHost.pipeServer.once('error',reject);
    });
    async function callPipe(packet) {
      const address=PluginClass._test.highlightPipeName(pipeHost.settings.token,pipeHost.settings.port);
      return new Promise((resolve,reject)=>{
        const socket=net.connect(address);
        const payload=Buffer.from(JSON.stringify(packet));
        const head=Buffer.alloc(4);head.writeUInt32BE(payload.length);
        let output=Buffer.alloc(0);
        socket.setTimeout(2500,()=>socket.destroy(new Error('pipe timeout')));
        socket.on('connect',()=> { socket.write(head); setImmediate(()=>socket.write(payload)); });
        socket.on('error',reject);
        socket.on('data',chunk=>{
          output=Buffer.concat([output,chunk]);
          if(output.length<4 || output.length<4+output.readUInt32BE(0))return;
          socket.destroy();resolve(JSON.parse(output.subarray(4).toString('utf8')));
        });
      });
    }
    try {
      assert.deepStrictEqual(await callPipe({method:'POST',route:'/highlight',
        token:'wrong',payload:{capture_id:'not-authorized'}}),
        {status:401,body:{error:'unauthorized'}});
      assert.strictEqual(handled,0);
      assert.deepStrictEqual(await callPipe({method:'POST',route:'/highlight',
        token:pipeHost.settings.token,payload:{capture_id:'fresh'}}),
        {status:200,body:{ok:true}});
      assert.strictEqual(handled,1);
      // Two complete frames on one socket: the old server ended after the first.
      const address=PluginClass._test.highlightPipeName(pipeHost.settings.token,pipeHost.settings.port);
      const two=await new Promise((resolve,reject)=>{
        const socket=net.connect(address);let pending=Buffer.alloc(0);const replies=[];
        socket.setTimeout(2500,()=>socket.destroy(new Error('reuse timeout')));
        socket.on('error',reject);
        socket.on('connect',()=>{
          const frames=[1,2].map(i=>{
            const body=Buffer.from(JSON.stringify({method:'POST',route:'/highlight',
              token:pipeHost.settings.token,payload:{capture_id:'reused-'+i}}));
            const head=Buffer.alloc(4);head.writeUInt32BE(body.length);return Buffer.concat([head,body]);
          });socket.write(Buffer.concat(frames));
        });
        socket.on('data',chunk=>{
          pending=Buffer.concat([pending,chunk]);
          while(pending.length>=4 && pending.length>=pending.readUInt32BE(0)+4){
            const length=pending.readUInt32BE(0);
            replies.push(JSON.parse(pending.subarray(4,4+length).toString('utf8')));
            pending=pending.subarray(4+length);
          }
          if(replies.length===2){socket.destroy();resolve(replies)}
        });
      });
      assert.deepStrictEqual(two.map(r=>r.status),[200,200]);
      assert.strictEqual(handled,3);
      const traced=await callPipe({method:'POST',route:'/highlight',
        token:pipeHost.settings.token,payload:{capture_id:'trace',trace:true}});
      assert.strictEqual(traced.status,200);
      assert.ok(traced.body.trace.pipe_first_data_callback_wall_ms
        <= traced.body.trace.pipe_data_callback_wall_ms);
      assert.ok(traced.body.trace.pipe_data_callback_wall_ms
        <= traced.body.trace.pipe_frame_dequeued_wall_ms);
      assert.ok(traced.body.trace.pipe_frame_dequeued_wall_ms
        <= traced.body.handler_wall_ms);
      assert.strictEqual((await callPipe({method:'GET',route:'/health',
        token:pipeHost.settings.token})).status,200);
    } finally {
      for(const socket of pipeHost.pipeSockets)socket.destroy();
      await new Promise(resolve=>pipeHost.pipeServer.close(resolve));
    }
  }

  // 调色板端点在 2.4.0 退役成兼容垫片：仍要求鉴权，但只回固定的 8 色、不再写盘——
  // 旧桌面端按老协议调用时不该收到 404 和「运行版本较旧，请重载」那句误导提示。
  const colorsHost = Object.create(PluginClass.prototype);
  colorsHost.settings = { token: 'color-token' };
  colorsHost.manifest = { version: '2.4.0' };
  let colorSaves = 0;
  colorsHost.saveData = async () => { colorSaves += 1; };
  const colorRequest = (token, value) => Object.assign(
    Readable.from([JSON.stringify({ colors: value })]),
    { method:'POST',url:'/highlight-colors',headers:{authorization:token} });
  let colorResponse = response();
  await colorsHost.handleRequest(colorRequest('Bearer wrong', { yellow:'#aa3300' }), colorResponse);
  assert.strictEqual(colorResponse.status, 401);
  colorResponse = response();
  await colorsHost.handleRequest(colorRequest('Bearer color-token', { yellow:'#aa3300' }), colorResponse);
  assert.strictEqual(colorResponse.status, 200);
  assert.strictEqual(colorResponse.body.fixed, true);
  assert.deepStrictEqual(colorResponse.body.colors, PluginClass._test.DEFAULT_HIGHLIGHT_PALETTE,
    '固定 8 色由插件给出，桌面端不再持有色值');
  assert.strictEqual(colorSaves, 0, '颜色固定后不再写 data.json');

  // 单条导出目标由桌面端推下来：索引文档必须是 Vault 内的 .md，注释目录不许越出 Vault。
  const singleHost = Object.create(PluginClass.prototype);
  singleHost.settings = { token: 'single-token' };
  let singleSaved = null;
  singleHost.saveData = async (value) => { singleSaved = { ...value }; };
  const singleRequest = (payload, token = 'Bearer single-token') => Object.assign(
    Readable.from([JSON.stringify(payload)]),
    { method:'POST',url:'/highlight-single-export-config',headers:{authorization:token} });
  let singleResponse = response();
  await singleHost.handleRequest(singleRequest({
    index_path: '问题库/问题索引.md', note_dir: '问题库/注释' }), singleResponse);
  assert.strictEqual(singleResponse.status, 200);
  assert.deepStrictEqual(singleResponse.body.config,
    { indexPath: '问题库/问题索引.md', noteDir: '问题库/注释' });
  assert.deepStrictEqual(singleSaved.highlightSingleExport, singleResponse.body.config,
    '配置要落到 data.json 才跨重启有效');
  // 2.5.0 去掉了「导出标题」：索引文档的标题改用文档自己的文件名，不该再有 title 字段。
  assert.ok(!('title' in singleResponse.body.config), '单条导出配置不再有 title');
  // 空串是合法的「还没配」，但给了值就必须是 Vault 内的相对路径。
  singleResponse = response();
  await singleHost.handleRequest(singleRequest({
    index_path: '../越界.md', note_dir: '问题库/注释' }), singleResponse);
  assert.strictEqual(singleResponse.status, 400);
  singleResponse = response();
  await singleHost.handleRequest(singleRequest({
    index_path: '问题库/问题索引.md', note_dir: 'C:/别处' }), singleResponse);
  assert.strictEqual(singleResponse.status, 400);
  singleResponse = response();
  await singleHost.handleRequest(singleRequest({
    index_path: '问题库/问题索引.md', note_dir: '问题库/注释' }, 'Bearer wrong'), singleResponse);
  assert.strictEqual(singleResponse.status, 401);

  // 悬浮窗摆哪 3 种颜色：桌面端推下来，必须是 8 色表里 3 个互不相同的键，否则 400。
  const activeHost = Object.create(PluginClass.prototype);
  activeHost.settings = { token: 'active-token', nativeHighlightColors: ['yellow','green','blue'] };
  let activeSaved = null;
  activeHost.saveData = async (value) => { activeSaved = { ...value }; };
  const activeRequest = (colors, token = 'Bearer active-token') => Object.assign(
    Readable.from([JSON.stringify({ colors })]),
    { method:'POST',url:'/highlight-active-colors',headers:{authorization:token} });
  let activeResponse = response();
  await activeHost.handleRequest(activeRequest(['red','purple','gray']), activeResponse);
  assert.strictEqual(activeResponse.status, 200);
  assert.deepStrictEqual(activeResponse.body.colors, ['red','purple','gray'], '按桌面端给的顺序生效');
  assert.deepStrictEqual(activeSaved.nativeHighlightColors, ['red','purple','gray'],
    '颜色选择要落到 data.json 才跨重启有效');
  // 悬浮窗和右键菜单都读这个，改动后立刻反映。
  assert.deepStrictEqual(activeHost.activeHighlightColors(), ['red','purple','gray']);
  // 数量不对、重复、或不是 8 色表里的键，一律 400——静默回退会让配置页显示的和实际对不上。
  for (const bad of [['red','purple'], ['red','purple','gray','blue'],
                     ['red','red','gray'], ['red','purple','chartreuse'], 'red', null]) {
    activeResponse = response();
    await activeHost.handleRequest(activeRequest(bad), activeResponse);
    assert.strictEqual(activeResponse.status, 400, `非法颜色集要被拒：${JSON.stringify(bad)}`);
  }
  // 2.3.x 的自定义调色板快照（green 槽其实是蓝）在归一化里按**实际看到的颜色**归类。
  activeResponse = response();
  await activeHost.handleRequest(activeRequest(['#0080ff','#00ff40','#e5484d']), activeResponse);
  assert.strictEqual(activeResponse.status, 200);
  assert.deepStrictEqual(activeResponse.body.colors, ['blue','green','red']);
  activeResponse = response();
  await activeHost.handleRequest(activeRequest(['red','purple','gray'], 'Bearer wrong'), activeResponse);
  assert.strictEqual(activeResponse.status, 401);
  assert.deepStrictEqual(PluginClass._test.normalizeActiveColors(undefined),
    PluginClass._test.DEFAULT_ACTIVE_COLORS, '没配过就是默认 3 色');
  assert.deepStrictEqual(PluginClass._test.DEFAULT_ACTIVE_COLORS, ['yellow','green','blue'],
    '默认 3 色正好是 2.3.x 那三个槽位的样子');

  // 连续两个不同选区写同一份 JSON，也要顺序落盘；旧的慢写不能覆盖新的。
  const serial = Object.create(PluginClass.prototype);
  serial.highlightStore = { version: 2, files: {} };
  serial.loadHighlights = async () => serial.highlightStore;
  serial.scheduleHighlightRefresh = () => {};
  let releaseFirst;
  const firstWrite = new Promise((resolve) => { releaseFirst = resolve; });
  const snapshots = [];
  serial.appendAnchorRecord = async (_filePath, anchor) => {
    snapshots.push(anchor.id);
    if (snapshots.length === 1) await firstWrite;
  };
  const writeA = serial.addAnchor("a.md", { id: "a", exact: "甲" });
  const writeB = serial.addAnchor("a.md", { id: "b", exact: "乙" });
  await new Promise((resolve) => setImmediate(resolve));
  assert.strictEqual(snapshots.length, 1);
  releaseFirst();
  await Promise.all([writeA, writeB]);
  assert.strictEqual(snapshots.length, 2);
  assert.deepStrictEqual(snapshots, ["a", "b"]);
  assert.deepStrictEqual(serial.anchorsForFile("a.md").map((a) => a.id), ["a", "b"]);

  // 本地目录只需首次创建；目录被外部移走时 ENOENT 安全重建并重试。
  const fs = require("fs"), os = require("os");
  const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), "nh-journal-"));
  assert.ok(path.resolve(tempRoot).startsWith(path.resolve(os.tmpdir())));
  const diskHost = Object.create(PluginClass.prototype);
  diskHost.manifest = { dir: "plugin" };
  diskHost.app = { vault: { adapter: { getBasePath: () => tempRoot } } };
  const diskDir = path.join(tempRoot, "plugin", "highlight-journal");
  const diskFile = path.join(diskDir, diskHost.highlightJournalName("课程/讲义.md"));
  try {
    await diskHost.appendAnchorRecord("课程/讲义.md", { id: "first" });
    await diskHost.appendAnchorRecord("课程/讲义.md", { id: "second" });
    assert.strictEqual(diskHost.lastHighlightMkdirMs, 0);
    assert.strictEqual(fs.readFileSync(diskFile, "utf8").split("\n").filter(Boolean).length, 2);
    fs.unlinkSync(diskFile);
    fs.rmdirSync(diskDir);
    await diskHost.appendAnchorRecord("课程/讲义.md", { id: "recovered" });
    assert.ok(fs.readFileSync(diskFile, "utf8").includes('"recovered"'));
  } finally {
    if (fs.existsSync(diskFile)) fs.unlinkSync(diskFile);
    if (fs.existsSync(diskDir)) fs.rmdirSync(diskDir);
    const pluginDir = path.join(tempRoot, "plugin");
    if (fs.existsSync(pluginDir)) fs.rmdirSync(pluginDir);
    fs.rmdirSync(tempRoot);
  }

  // ---- 数据安全边界（源码级守卫）----
  //
  // /highlight 只记锚点，**不能**再去改用户的文件。这一条是这一轮的核心改动，
  // 一旦被后人改回去（又去写 <mark> 或插块 ID），下面会立刻红。
  const sourceText = require("fs").readFileSync(
    path.join(__dirname, "..", "obsidian-plugin", "noting-helper-bridge", "source.js"),
    "utf8",
  );
  const startAt = sourceText.indexOf("async applyHighlight(");
  assert.ok(startAt > 0, "找不到 applyHighlight");
  const body = sourceText.slice(startAt, sourceText.indexOf("\nmodule.exports"));
  for (const forbidden of ["vault.modify", "replaceRange", "highlightMarkup"]) {
    assert.ok(
      !body.includes(forbidden),
      `/highlight 不该再碰文件，却出现了 ${forbidden}`,
    );
  }
  assert.ok(body.includes("addAnchor"), "/highlight 应当记锚点");

  // readPageText 必须接**文本节点**。踩过：以前接的是 .textLayer 里的 <span>，
  // 于是后面 Range.setStart(span, 437) —— offset 对元素节点是「第几个子节点」
  // （span 只有 1 个），对文本节点才是字符偏移。那些 Range 全非法，一段颜色都
  // 画不出来，**而且不报错**。这条断言挡住「改回传 span」。
  assert.ok(
    !sourceText.includes("readPageText(Array.from("),
    "readPageText 不能接元素列表，只能接文本节点",
  );
  assert.ok(
    sourceText.includes("readPageText(textNodesOf("),
    "PDF 文本层应当经 textNodesOf 取文本节点",
  );

  // readPageText 要认 nodeValue（文本节点上才有这个属性）
  const textNodeParts = readPageText([
    { nodeType: 3, nodeValue: "甲乙" },
    { nodeType: 3, nodeValue: "丙丁" },
  ]);
  assert.strictEqual(textNodeParts.text, "甲乙丙丁");
  assert.strictEqual(textNodeParts.parts.length, 2);
  assert.strictEqual(textNodeParts.parts[1].start, 2);
  assert.strictEqual(textNodeParts.parts[1].end, 4);
  // 记录问题的「标注来源」仍然要插块 ID，那条路没被动过
  assert.ok(
    sourceText.includes("activeEditor.replaceRange(insertion"),
    "createAnchor 里插块 ID 的逻辑不该被这一轮带走",
  );

  // 单个 JSONL 迁移：快照重置旧分片，注释/颜色与原文保存在同一文件。
  const isolated = fakeStoreHost(JSON.stringify({ version: 2, files: {} }));
  await isolated.addAnchor('课程/第一章.md', {kind:'md',exact:'重点',start:3,end:5,
    prefix:'',suffix:'',color:'yellow',id:'hl-1'});
  const storage = Object.create(PluginClass.prototype);
  storage.manifest = isolated.manifest;
  storage.app = isolated.app;
  storage.app.vault.getName = () => '测试仓库';
  storage.app.vault.adapter.exists = async (file) => file in isolated.state.files;
  storage.settings = { ...isolated.settings };
  storage.highlightStore = isolated.highlightStore;
  const opened = [{view:{file:{path:'课程/第一章.md'}}}];
  storage.app.workspace = {iterateAllLeaves(callback) { opened.forEach(callback); }};
  storage.openAnnotationCache = new Map();
  storage.syncOpenAnnotationCache();
  assert.strictEqual(storage.openAnnotationCache.get('课程/第一章.md').size,0);
  storage.pendingAnchorEdits = new Map();
  storage.saveData = async () => {};
  storage.refreshEditorDecorations = () => {};
  storage.scheduleHighlightRefresh = () => {};
  await storage.changeHighlight('hl-1',{note:'含 #标签 的注释',tags:['#标签']});
  assert.strictEqual(storage.annotationForHighlight('hl-1').note,'含 #标签 的注释');
  const appendBeforeFailure = storage.appendHighlightRecord;
  storage.appendHighlightRecord = async () => { throw new Error('模拟写盘失败'); };
  await assert.rejects(storage.changeHighlight('hl-1',{note:'不应可见'}),/模拟写盘失败/);
  assert.strictEqual(storage.annotationForHighlight('hl-1').note,'含 #标签 的注释',
    '写盘失败时缓存与源状态均不能提前变更');
  storage.appendHighlightRecord = appendBeforeFailure;
  await storage.changeHighlight('hl-1',{note:'新注释',images:['图片/1.png']});
  assert.deepStrictEqual(storage.annotationForHighlight('hl-1').images,['图片/1.png']);
  await storage.changeHighlight('hl-1',{note:'含 #标签 的注释',images:[]});
  assert.strictEqual(storage.annotationForHighlight('hl-1').note,'含 #标签 的注释');
  await storage.changeHighlight('hl-1',{color:'green'});
  opened.push({view:{file:{path:'课程/第一章.md'}}});
  opened.shift();
  storage.syncOpenAnnotationCache();
  assert.strictEqual(storage.openAnnotationCache.size,1,'分屏仍有文件打开时继续保留缓存');
  opened.length=0;
  storage.syncOpenAnnotationCache();
  assert.strictEqual(storage.openAnnotationCache.size,0,'关闭文件后释放派生注释缓存');
  opened.push({view:{file:{path:'课程/第一章.md'}}});
  storage.syncOpenAnnotationCache();
  assert.strictEqual(storage.annotationForHighlight('hl-1').note,'含 #标签 的注释');
  assert.strictEqual(storage.anchorsForFile('课程/第一章.md')[0].color,'green');
  const oldLog = isolated.state.files[isolated.settings.highlightJournal];
  assert.ok(oldLog.includes('"patches"'));
  assert.throws(()=>storage.checkedStoragePath('../outside.jsonl'),/相对路径/);
  assert.throws(()=>storage.checkedStoragePath('C:/outside.jsonl'),/相对路径/);
  assert.strictEqual(storage.settings.highlightJournal, isolated.fixedHighlightJournal);
  assert.ok(isolated.state.files[isolated.settings.highlightJournal].length >= oldLog.length);
  const reloadedNotes = fakeStoreHost(undefined, isolated.state);
  await reloadedNotes.loadHighlights();
  assert.deepStrictEqual(reloadedNotes.anchorsForFile('课程/第一章.md')[0].tags,['#标签']);
  const exported = storage.exportHighlights('NoteDataOfY/高光库/导出');
  assert.strictEqual(exported.count,1);
  assert.strictEqual(exported.files.length,2);
  const indexEntry=exported.files.find(({path})=>path.startsWith(`${exported.folder}/索引/`));
  const noteEntry=exported.files.find(({path})=>path.startsWith(`${exported.folder}/注释/`));
  assert.ok(indexEntry.path.endsWith('/第一章__md.md'));
  // 注释目录名不是高光 ID：没设注释标题时取原文前几个字（这条的原文是「重点」）。
  assert.ok(noteEntry.path.endsWith('/第一章__md/重点/注释.md'));
  assert.ok(indexEntry.text.includes(noteEntry.path.slice(0,-3)));
  assert.ok(noteEntry.text.includes('含 #标签 的注释'));
  assert.ok(indexEntry.text.includes('obsidian://noting-helper-highlight'));
  await storage.writeHighlightExport('NoteDataOfY/高光库/导出');
  assert.ok(isolated.state.files[noteEntry.path].includes('含 #标签 的注释'));
  await storage.changeHighlight('hl-1',{},true);
  assert.strictEqual(storage.annotationForHighlight('hl-1'),null);
  assert.strictEqual(storage.openAnnotationCache.get('课程/第一章.md').size,0);
  assert.strictEqual(storage.exportHighlights('NoteDataOfY/高光库/导出').count,0);
  const afterDelete = fakeStoreHost(undefined,isolated.state);
  await afterDelete.loadHighlights();
  assert.strictEqual(afterDelete.anchorsForFile('课程/第一章.md').length,0);

  // 真实文件系统：旧 JSONL 有 reset 时，迁入插件私有库绝不复活旧 JSON。
  const migrationRoot=fs.mkdtempSync(path.join(os.tmpdir(),'nh-private-migrate-'));
  try {
    const dir='.obsidian/plugins/noting-helper-bridge';
    const old='NoteDataOfY/高光库/高光数据.jsonl';
    const fixed=`${dir}/storage/highlights.jsonl`;
    const oldAbs=path.join(migrationRoot,...old.split('/'));
    const sourceAbs=path.join(migrationRoot,'来源.md');
    fs.mkdirSync(path.dirname(oldAbs),{recursive:true});
    fs.writeFileSync(oldAbs,JSON.stringify({reset:true,at:1})+'\n');
    fs.writeFileSync(sourceAbs,'原文不改');
    const legacyAbs=path.join(migrationRoot,...`${dir}/highlights.json`.split('/'));
    fs.mkdirSync(path.dirname(legacyAbs),{recursive:true});
    fs.writeFileSync(legacyAbs,JSON.stringify({files:{'来源.md':[{id:'hl-old',kind:'md',exact:'原文'}]}}));
    const makeHost=(journal)=>{
      const h=Object.create(PluginClass.prototype);
      h.manifest={dir};h.settings={highlightJournal:journal};h.fixedHighlightJournal=fixed;
      h.app={vault:{adapter:{getBasePath:()=>migrationRoot,
        exists:async rel=>fs.existsSync(path.join(migrationRoot,...rel.split('/'))),
        read:async rel=>fs.promises.readFile(path.join(migrationRoot,...rel.split('/')),'utf8'),
        list:async rel=>({files:fs.readdirSync(path.join(migrationRoot,...rel.split('/')))
          .map(name=>`${rel}/${name}`)})}}};
      h.openAnnotationCache=new Map();h.pendingAnchorEdits=new Map();h.saveData=async()=>{};
      return h;
    };
    const migrated=makeHost(old);
    await migrated.loadHighlights();
    assert.strictEqual(migrated.settings.highlightJournal,fixed);
    assert.strictEqual(migrated.activeHighlightCount(),0);
    assert.deepStrictEqual(JSON.parse(fs.readFileSync(path.join(migrationRoot,...fixed.split('/')),'utf8').trim()).reset,true);
    assert.ok(!fs.existsSync(oldAbs),'旧笔记区 JSONL 应归档进插件备份');
    assert.ok(fs.readdirSync(path.join(migrationRoot,...`${dir}/backup`.split('/')))
      .some(name=>name.startsWith('highlight-migration-')&&name.endsWith('.jsonl')));
    assert.strictEqual(fs.readFileSync(sourceAbs,'utf8'),'原文不改');
    const again=makeHost(fixed);
    again.app.vault.adapter.read=async rel=>{
      assert.strictEqual(rel,fixed,'有内部主库时不能再读旧库');
      return fs.promises.readFile(path.join(migrationRoot,...rel.split('/')),'utf8');
    };
    await again.loadHighlights();assert.strictEqual(again.activeHighlightCount(),0);
  } finally { fs.rmSync(migrationRoot,{recursive:true,force:true}); }

  // 重复导出：索引按文档名一篇、注释按注释名建目录，过期生成文件与空目录可回收。
  const exportRoot=fs.mkdtempSync(path.join(os.tmpdir(),'nh-tree-export-'));
  try {
    const dir='.obsidian/plugins/noting-helper-bridge';
    const picture=`${dir}/storage/imagehighlights/image-1.png`;
    fs.mkdirSync(path.dirname(path.join(exportRoot,...picture.split('/'))),{recursive:true});
    fs.writeFileSync(path.join(exportRoot,...picture.split('/')),Buffer.from([1,2,3]));
    const e=Object.create(PluginClass.prototype);e.manifest={dir};
    e.app={vault:{getName:()=> '测试仓库',adapter:{getBasePath:()=>exportRoot,
      exists:async rel=>fs.existsSync(path.join(exportRoot,...rel.split('/'))),
      read:async rel=>fs.promises.readFile(path.join(exportRoot,...rel.split('/')),'utf8')}}};
    e.pendingAnchorEdits=new Map();
    e.highlightStore={files:{'来源/a.md':[
        {id:'hl-a',kind:'md',exact:'一',color:'yellow',note:'前\n后',
         noteLayout:'前\n[图片:图.png]\n后',images:[picture],imageNames:['图.png']},
        {id:'hl-a2',kind:'md',exact:'二',color:'blue',title:'高:分/句型?',note:'带标题的注释'},
        {id:'hl-a3',kind:'md',exact:'三',color:'blue',title:'高 分 句型',note:'标题去符号后撞名'},
        {id:'hl-a4',kind:'md',exact:'四五六七八九一二三',color:'blue',note:'没设标题'}],
      '来源/a.pdf':[{id:'hl-b',kind:'pdf',exact:'二',page:3,color:'green'}]}};
    const folder='导出';const plan=e.exportHighlights(folder);
    assert.strictEqual(plan.count,5);
    // 不带颜色落在「所有高光」档：导出根下先分档再铺文档，两档的清单互不干涉。
    assert.strictEqual(plan.scope,'所有高光');
    assert.strictEqual(plan.folder,'导出/所有高光');
    const scope='导出/所有高光';
    // 来源的父目录（讲义.pdf 本来是 文档资料库/<哈希>/讲义.pdf）不再镜像进导出树：
    // 索引平铺成「文档名__扩展名」一篇，注释按同一层文档名归拢。
    assert.ok(plan.files.every(({path})=>!path.includes('/来源/')),'导出树不镜像来源父目录');
    assert.ok(plan.files.some(x=>x.path===`${scope}/索引/a__md.md`));
    assert.ok(plan.files.some(x=>x.path===`${scope}/索引/a__pdf.md`));
    // 没设注释标题的一条用原文前几个字当目录名。
    assert.ok(plan.files.some(x=>x.path===`${scope}/注释/a__md/一/注释.md`));
    assert.ok(plan.files.some(x=>x.path===`${scope}/注释/a__md/四五六七八九一二三/注释.md`));
    // 设了标题的一条用标题当目录名：路径不安全字符先换成空格、多余空格合并；
    // 去掉符号后与下一条同名的，按顺序补 -2，两条注释各自留在自己的目录里。
    assert.ok(plan.files.some(x=>x.path===`${scope}/注释/a__md/高 分 句型/注释.md`));
    assert.ok(plan.files.some(x=>x.path===`${scope}/注释/a__md/高 分 句型-2/注释.md`));
    const note=plan.files.find(x=>x.path===`${scope}/注释/a__md/一/注释.md`);
    assert.ok(note.text.indexOf('前')<note.text.indexOf('![图.png]'));
    assert.ok(note.text.indexOf('![图.png]')<note.text.indexOf('后'));
    // 所有高光档的标题不带颜色，每条记录自己标颜色：黄绿蓝混在一篇里也分得清。
    const allIndex=plan.files.find(x=>x.path===`${scope}/索引/a__md.md`).text;
    assert.ok(allIndex.startsWith('# a__md · 高光索引\n'),'所有高光档的标题不带颜色');
    assert.ok(allIndex.includes('- 颜色：黄 🟡'));
    assert.ok(allIndex.includes('- 颜色：蓝 🔵'));
    // 按颜色分档：只收这一档的记录，进自己的目录、维护自己的清单。
    const bluePlan=e.exportHighlights(folder,{color:'blue'});
    assert.strictEqual(bluePlan.scope,'高光-蓝');
    assert.strictEqual(bluePlan.folder,'导出/高光-蓝');
    assert.strictEqual(bluePlan.count,3,'只有蓝的那三条进蓝档');
    assert.deepStrictEqual([...new Set(bluePlan.files.map(f=>f.path))].sort(),[
      '导出/高光-蓝/注释/a__md/四五六七八九一二三/注释.md',
      '导出/高光-蓝/注释/a__md/高 分 句型-2/注释.md',
      '导出/高光-蓝/注释/a__md/高 分 句型/注释.md',
      '导出/高光-蓝/索引/a__md.md',
    ],'本档没有高光的来源文档不生成空索引，清单也不抖动');
    const blueIndex=bluePlan.files.find(x=>x.path==='导出/高光-蓝/索引/a__md.md').text;
    assert.ok(blueIndex.startsWith('# a__md · 高光索引（蓝 🔵）\n'),'分档导出的标题带这一档的颜色');
    assert.ok(!/- 高光 ID：hl-a\n/.test(blueIndex),'黄的那条不进蓝档索引');
    assert.ok(!blueIndex.includes('hl-b'),'绿的那条不进蓝档索引');
    assert.ok(blueIndex.includes('- 高光 ID：hl-a2'));
    const userIndex=plan.files.find(({path})=>path.endsWith('索引/a__md.md')).path;
    const userIndexAbs=path.join(exportRoot,...userIndex.split('/'));
    fs.mkdirSync(path.dirname(userIndexAbs),{recursive:true});
    fs.writeFileSync(userIndexAbs,'# 手工笔记');
    await assert.rejects(()=>e.writeHighlightExport(folder),/不是本插件生成/);
    fs.unlinkSync(userIndexAbs);
    await e.writeHighlightExport(folder);
    await e.writeHighlightExport(folder,{color:'blue'});
    assert.strictEqual(fs.existsSync(path.join(exportRoot,...'导出/所有高光/.noting-helper-export.json'.split('/'))),true);
    assert.strictEqual(fs.existsSync(path.join(exportRoot,...'导出/高光-蓝/.noting-helper-export.json'.split('/'))),true);
    assert.ok(fs.existsSync(path.join(exportRoot,...plan.images[0].target.split('/'))));
    e.highlightStore.files['来源/a.md']=[];
    await e.writeHighlightExport(folder);
    assert.ok(!fs.existsSync(path.join(exportRoot,...note.path.split('/'))));
    assert.ok(!fs.existsSync(path.join(exportRoot,...plan.images[0].target.split('/'))));
    assert.ok(fs.existsSync(path.join(exportRoot,...`${scope}/索引/a__pdf.md`.split('/'))));
    // 全清了以后蓝档自己也空掉，但「所有高光」档的清单还在——清理只扫自己那一档。
    assert.ok(fs.existsSync(path.join(exportRoot,...'导出/高光-蓝/.noting-helper-export.json'.split('/'))));
    // 目录名跟着标题走，改标题就换目录；回收文件后要一路删掉空目录，否则导出树里
    // 会留下一串空壳。往上删到非空目录为止（索引还在，所以 索引 那一层必须留着）。
    assert.ok(!fs.existsSync(path.join(exportRoot,...`${scope}/注释/a__md/一`.split('/'))));
    assert.ok(!fs.existsSync(path.join(exportRoot,...`${scope}/注释/a__md`.split('/'))));
    assert.ok(fs.existsSync(path.join(exportRoot,...`${scope}/索引`.split('/'))));
  } finally { fs.rmSync(exportRoot,{recursive:true,force:true}); }

  // 颜色内核：8 张固定色卡之外，历史快照按「用户当年实际看到的颜色」归类。
  const {highlightColorKey,highlightColorDef,anchorColorHex,anchorColorLabel,anchorColorDot,
    mergeIndexRecord,DEFAULT_HIGHLIGHT_PALETTE}=PluginClass._test;
  assert.deepStrictEqual(Object.keys(DEFAULT_HIGHLIGHT_PALETTE),[
    'red','orange','yellow','green','blue','purple','magenta','gray'],'固定的 8 色，顺序也是契约');
  // 2.3.x 的自定义调色板把 green 槽画成蓝、blue 槽画成绿，快照按真实颜色归类。
  assert.strictEqual(highlightColorKey({colorHex:'#ff00ff'}),'magenta');
  assert.strictEqual(highlightColorKey({colorHex:'#0080ff'}),'blue');
  assert.strictEqual(highlightColorKey({colorHex:'#00ff40'}),'green');
  // 快照优先：color 键可能是旧调色板留下的、和画出来的颜色对不上的标签（用户那里
  // yellow 存着 #ff00ff），两边都在色表里且不一致时以快照为准，升级后视觉零变化。
  assert.strictEqual(highlightColorKey({color:'yellow',colorHex:'#ff00ff'}),'magenta');
  assert.strictEqual(anchorColorHex({color:'yellow',colorHex:'#ff00ff'}),'#ff00ff');
  assert.strictEqual(anchorColorLabel({color:'yellow',colorHex:'#ff00ff'}),'品红');
  // 快照认不出时才退回键；两个都不认识兜到 yellow，绝不让某条高光没有颜色。
  assert.strictEqual(highlightColorKey({color:'blue',colorHex:'#123456'}),'blue');
  assert.strictEqual(anchorColorHex({color:'blue',colorHex:'#123456'}),'#086ddd');
  assert.strictEqual(highlightColorKey({color:'没这个色'}),'yellow');
  assert.strictEqual(highlightColorKey(null),'yellow');
  assert.strictEqual(highlightColorDef('green').hex,'#08b94e');
  assert.strictEqual(anchorColorDot({color:'blue'}),'🔵');

  // 导出菜单的可选项每次现算：数量为 0 的颜色不出现在菜单里，由桌面端按 count 过滤。
  const optionsHost=Object.create(PluginClass.prototype);
  optionsHost.manifest={version:'2.4.0'};
  optionsHost.settings={token:'options-token',highlightSingleExport:
    {indexPath:'问题库/问题索引.md',noteDir:'问题库/注释',title:'记录'}};
  optionsHost.highlightStore={files:{'a.md':[{id:'1',color:'yellow'},{id:'2',colorHex:'#ff00ff'},
    {id:'3',color:'blue'}],'b.pdf':[{id:'4',color:'blue'}]}};
  const options=optionsHost.highlightExportOptions();
  assert.strictEqual(options.total,4);
  assert.deepStrictEqual(options.colors.map(c=>[c.key,c.count]),
    [['red',0],['orange',0],['yellow',1],['green',0],['blue',2],['purple',0],['magenta',1],['gray',0]]);
  assert.strictEqual(options.colors.reduce((sum,c)=>sum+c.count,0),options.total,
    '每色计数之和必须等于总数，否则菜单会漏掉高光');
  assert.deepStrictEqual(options.single_export,optionsHost.settings.highlightSingleExport,
    '单条导出目标一并回给桌面端');
  const optionsResponse=response();
  await optionsHost.handleRequest(Object.assign(Readable.from([]),
    {method:'GET',url:'/highlight-export-options',headers:{authorization:'Bearer options-token'}}),
    optionsResponse);
  assert.strictEqual(optionsResponse.status,200);
  assert.deepStrictEqual(optionsResponse.body.colors,options.colors);
  const optionsDenied=response();
  await optionsHost.handleRequest(Object.assign(Readable.from([]),
    {method:'GET',url:'/highlight-export-options',headers:{authorization:'Bearer wrong'}}),
    optionsDenied);
  assert.strictEqual(optionsDenied.status,401);
  // 分档导出只认 8 色之一，别的一律 400，不能把整棵树导到一个不认识的名字下。
  const badColor=response();
  await optionsHost.handleRequest(Object.assign(
    Readable.from([JSON.stringify({directory:'导出',color:'chartreuse'})]),
    {method:'POST',url:'/highlight-export',headers:{authorization:'Bearer options-token'}}),badColor);
  assert.strictEqual(badColor.status,400);

  // 并进用户自己维护的索引文档：标记由合并函数自己加，块只按高光 ID 定位。
  const mergedBlock=['## 例句','','- 高光 ID：hl-merge'].join('\n');
  const appended=mergeIndexRecord('# 问题索引\n\n用户自己的一段\n','hl-merge',mergedBlock,'\n');
  assert.ok(appended.startsWith('# 问题索引\n\n用户自己的一段\n'),'用户内容原样不动');
  assert.ok(appended.includes('<!-- nh:begin hl-merge -->\n## 例句\n\n- 高光 ID：hl-merge\n<!-- nh:end hl-merge -->'),
    '块用 HTML 注释括起来：阅读视图不可见、源码可定位');
  const again=mergeIndexRecord(appended,'hl-merge','## 换个标题\n','\n');
  assert.strictEqual((again.match(/nh:begin hl-merge/g)||[]).length,1,'重复导出不产生第二份');
  assert.ok(again.includes('## 换个标题'));
  assert.ok(!again.includes('## 例句'),'改标题是就地替换，位置不动');
  assert.ok(again.includes('用户自己的一段'),'替换只覆盖自己的块');
  // CRLF 保真：用户是 Windows，混行尾会让整篇显示成变更。
  const crlf=mergeIndexRecord('# 问题索引\r\n\r\n用户的一段\r\n','hl-merge',mergedBlock,'\r\n');
  assert.ok(crlf.includes('\r\n<!-- nh:begin hl-merge -->\r\n'),'块内换行跟随文件既有的行尾');
  assert.ok(!/[^\r]\n/.test(crlf),'不能留下裸 \\n');
  assert.strictEqual(mergeIndexRecord(crlf,'hl-merge',mergedBlock,'\r\n'),crlf,'幂等');
  // 别的 id 的块（用户手抄的、或另一条高光）不受影响。
  const other=mergeIndexRecord(appended,'hl-other','## 别人的\n','\n');
  assert.ok(other.includes('nh:begin hl-merge')&&other.includes('nh:begin hl-other'));
  assert.ok(other.includes('## 例句'),'不是自己的块不动');

  // 单条导出：注释落在 <注释目录>/<注释名>/注释.md（比批量少一层），索引并进配置的文档。
  const singleRoot=fs.mkdtempSync(path.join(os.tmpdir(),'nh-single-export-'));
  try {
    const s=Object.create(PluginClass.prototype);s.manifest={dir:'.obsidian/plugins/noting-helper-bridge'};
    s.settings={highlightSingleExport:{indexPath:'问题库/问题索引.md',noteDir:'问题库/注释',title:'问题记录'}};
    s.app={vault:{getName:()=> '测试仓库',adapter:{
      getBasePath:()=>singleRoot,
      exists:async rel=>fs.existsSync(path.join(singleRoot,...rel.split('/'))),
      read:async rel=>fs.promises.readFile(path.join(singleRoot,...rel.split('/')),'utf8')}}};
    s.pendingAnchorEdits=new Map();
    s.highlightStore={files:{'来源/a.md':[{id:'hl-s',kind:'md',exact:'原文一句',color:'blue',
      title:'我的题目',note:'我的解释',tags:['复习','重点']}]}};
    s.flushAnchorEdits=async()=>{};
    const absolute=rel=>path.join(singleRoot,...rel.split('/'));
    fs.mkdirSync(path.dirname(absolute('问题库/问题索引.md')),{recursive:true});
    fs.writeFileSync(absolute('问题库/问题索引.md'),'# 我的问题记录\r\n\r\n- 第一条\r\n');
    const single=await s.exportSingleHighlight('hl-s');
    assert.strictEqual(single.note,'问题库/注释/我的题目/注释.md','比批量少 文档名__扩展名 一层');
    assert.ok(fs.existsSync(absolute(single.note)));
    const singleIndex=fs.readFileSync(absolute('问题库/问题索引.md'),'utf8');
    assert.ok(singleIndex.startsWith('# 我的问题记录\r\n\r\n- 第一条\r\n'),'用户原有内容不动');
    assert.ok(!/[^\r]\n/.test(singleIndex),'追加到 CRLF 文档里不留裸 \\n');
    assert.ok(singleIndex.includes('- 颜色：蓝 🔵'));
    assert.ok(singleIndex.includes('[[问题库/注释/我的题目/注释|查看注释]]'));
    assert.ok(singleIndex.includes('- 来源：[[来源/a.md]]'),'来源行直接贴文档本身');
    assert.ok(!singleIndex.includes('打开文档'),'「打开文档」别名已去掉，后面直接跟定位到高光');
    // 标签必须是 Obsidian 认的行内标签：纯文本写在列表里等于没写，不进标签面板也点不动。
    // 「标签：」后面那个空格是硬要求——行内标签的 `#` 前面必须是空白或行首，
    // `- 标签：#复习` 里 `#` 紧跟全角冒号，Obsidian 认不出来，写了等于没写。
    const singleNoteText=fs.readFileSync(absolute(single.note),'utf8');
    assert.ok(singleNoteText.includes('- 标签： #复习 #重点'),'标签写成 #标签 才有 Obsidian 语义');
    for(const line of singleNoteText.split('\n')){
      for(const tag of line.match(/#[\p{L}\p{N}_\-/]+/gu)||[]){
        const at=line.indexOf(tag);
        assert.ok(at===0||/\s/.test(line[at-1]),
          `标签的 # 前面必须是行首或空白，不能是标点：${line}`);
      }
    }
    assert.ok(singleNoteText.includes('- 来源：[[来源/a.md]]'),'注释里的来源行同样不再套别名');
    // 再导一次同一条：就地替换，不产生第二份。
    const second=await s.exportSingleHighlight('hl-s');
    assert.strictEqual(second.note,single.note);
    assert.strictEqual(fs.readFileSync(absolute('问题库/问题索引.md'),'utf8'),singleIndex,'重复导出是幂等的');
    // 注释目录里躺着别人（不是我们生成的）的注释.md 时改名，绝不覆盖。
    fs.mkdirSync(path.dirname(absolute('问题库/注释/第二题/注释.md')),{recursive:true});
    fs.writeFileSync(absolute('问题库/注释/第二题/注释.md'),'用户自己的笔记');
    s.highlightStore.files['来源/a.md'][0].title='第二题';
    const renamed=await s.exportSingleHighlight('hl-s');
    assert.strictEqual(renamed.note,'问题库/注释/第二题-2/注释.md','撞上用户的笔记就改名，不抛错也不覆盖');
    assert.strictEqual(fs.readFileSync(absolute('问题库/注释/第二题/注释.md'),'utf8'),'用户自己的笔记');
    // 索引文档不存在时新建：一级标题用索引文档自己的文件名，不再单独配一个「导出标题」。
    s.settings={highlightSingleExport:{indexPath:'问题库/新索引.md',noteDir:'问题库/注释2'}};
    await s.exportSingleHighlight('hl-s');
    assert.ok(fs.readFileSync(absolute('问题库/新索引.md'),'utf8').startsWith('# 新索引\n'),
      '新建时的一级标题就是索引文档的文件名');
    // 没设注释标题时，索引里的标题取原文前 24 字——与注释目录名同源（annotationTitle）。
    const longExact='零一二三四五六七八九十'.repeat(3);
    delete s.highlightStore.files['来源/a.md'][0].title;
    s.highlightStore.files['来源/a.md'][0].exact=longExact;
    await s.exportSingleHighlight('hl-s');
    assert.ok(fs.readFileSync(absolute('问题库/新索引.md'),'utf8').includes(`## ${longExact.slice(0,24)}`),
      '没标题就取原文前 24 字');
    // 标签清洗：去掉已有的 #、非法字符换成连字符，空标签丢掉。
    const {tagMarkup,annotationTitle}=PluginClass._test;
    assert.strictEqual(tagMarkup(['复习',' 重点 ']),'#复习 #重点');
    assert.strictEqual(tagMarkup(['#已有','带 空 格']),'#已有 #带-空-格');
    // 标签字符集只认字母/数字/_/-//（与编辑器的 `#[\p{L}\p{N}_\-/]+` 同一套）：
    // `#a.b` 在 Obsidian 里只剩 `#a`，得在导出前就把 `b` 接回来。
    assert.strictEqual(tagMarkup(['a.b','x：y','p#q']),'#a-b #x-y #p-q');
    assert.strictEqual(tagMarkup(['.','  ','##']),'');
    assert.strictEqual(tagMarkup([]),'');
    assert.strictEqual(tagMarkup(undefined),'');
    assert.strictEqual(annotationTitle({title:'  ',exact:'  原文\n第二行  '},24),'原文','空标题回落到原文首行');
    // 没配目标就明确拒绝，不能悄悄写到一个猜出来的位置。
    s.settings={};
    await assert.rejects(()=>s.exportSingleHighlight('hl-s'),/尚未配置单条导出目标/);
    await assert.rejects(()=>s.exportSingleHighlight('没这个 id'),/已不存在/);
    // Two real vaults: neither export mode may mutate the source vault's old index.
    const otherRoot=fs.mkdtempSync(path.join(os.tmpdir(),'nh-cross-export-'));
    try {
      fs.mkdirSync(path.join(otherRoot,'.obsidian'));
      const dest=rel=>path.join(otherRoot,...rel.split('/'));
      const anchor=s.highlightStore.files['来源/a.md'][0];
      anchor.title='跨库注释';anchor.images=['图片/example.png'];anchor.imageNames=['example.png'];
      fs.mkdirSync(path.dirname(absolute(anchor.images[0])),{recursive:true});
      fs.writeFileSync(absolute(anchor.images[0]),Buffer.from([1,2,3,4]));
      s.saveData=async()=>{};
      const before=fs.readFileSync(absolute('问题库/新索引.md'),'utf8');
      const config=await s.configureHighlightSingleExport({index_path:'索引/另一库.md',note_dir:'正文库',vault_path:otherRoot});
      assert.strictEqual(config.vaultPath,otherRoot);
      const result=await s.exportSingleHighlight('hl-s');
      assert.strictEqual(result.vault_path,otherRoot);
      assert.ok(fs.existsSync(dest('索引/另一库.md')));
      const crossText=fs.readFileSync(dest(result.note),'utf8');
      assert.ok(crossText.includes('obsidian://open?vault='+encodeURIComponent('测试仓库')+'&file='));
      assert.ok(crossText.includes('obsidian://noting-helper-highlight?vault='+encodeURIComponent('测试仓库')));
      assert.deepStrictEqual(fs.readFileSync(dest(path.posix.dirname(result.note)+'/image/1-example.png')),Buffer.from([1,2,3,4]));
      assert.strictEqual(fs.readFileSync(absolute('问题库/新索引.md'),'utf8'),before);
      const batch=await s.writeHighlightExport('全部导出',{vaultPath:otherRoot});
      assert.strictEqual(batch.vault_path,otherRoot);
      assert.strictEqual(batch.count,1);
      const listing=s.exportHighlights('全部导出',{crossVault:true});
      for(const entry of listing.files) assert.ok(fs.existsSync(dest(entry.path)));
      assert.ok(fs.readFileSync(dest(listing.files[0].path),'utf8').includes('obsidian://open?vault='));
      assert.ok(!fs.existsSync(absolute(batch.folder)),'全量导出不能误写来源库');
      assert.deepStrictEqual(fs.readFileSync(dest(listing.images[0].target)),Buffer.from([1,2,3,4]));
      await s.writeHighlightExport('全部导出',{vaultPath:otherRoot});
      fs.writeFileSync(dest(listing.files[0].path),'用户自己写的正文');
      await assert.rejects(()=>s.writeHighlightExport('全部导出',{vaultPath:otherRoot}),/不是本插件生成/);
      assert.strictEqual(fs.readFileSync(dest(listing.files[0].path),'utf8'),'用户自己写的正文');
      await assert.rejects(()=>s.configureHighlightSingleExport({index_path:'../坏.md',note_dir:'正文库',vault_path:otherRoot}));
      await assert.rejects(()=>s.configureHighlightSingleExport({index_path:'索引/好.md',note_dir:'正文库',vault_path:singleRoot}));
      assert.strictEqual(s.settings.highlightSingleExport.vaultPath,otherRoot,'坏配置不得替换有效目标');
      // A junction/symlink inside the destination must not grant access outside it.
      const escape=path.join(otherRoot,'逃逸链接');
      fs.symlinkSync(singleRoot,escape,process.platform==='win32'?'junction':'dir');
      await assert.rejects(()=>s.configureHighlightSingleExport({index_path:'逃逸链接/坏.md',note_dir:'正文库',vault_path:otherRoot}),/边界/);
      await assert.rejects(()=>s.writeHighlightExport('逃逸链接',{vaultPath:otherRoot}),/边界/);
      assert.ok(!fs.existsSync(absolute('坏.md')));
      // Clearing the target removes the previous destination as well.
      await s.configureHighlightSingleExport({index_path:'',note_dir:''});
      assert.ok(!s.settings.highlightSingleExport.vaultPath);
    } finally { fs.rmSync(otherRoot,{recursive:true,force:true}); }
  } finally { fs.rmSync(singleRoot,{recursive:true,force:true}); }

  // 本地 Vault 的全量清空：先备份原 JSONL，再原子写 reset；旧只读库不复活。
  const resetRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'nh-reset-'));
  try {
    const journalRel='.obsidian/plugins/noting-helper-bridge/storage/highlights.jsonl';
    const absolute=path.join(resetRoot,...journalRel.split('/'));
    const legacyRel='.obsidian/plugins/noting-helper-bridge/highlights.json';
    const legacyAbsolute=path.join(resetRoot,...legacyRel.split('/'));
    const sourcePdf=path.join(resetRoot,'讲义.pdf');
    fs.mkdirSync(path.dirname(absolute),{recursive:true});
    fs.mkdirSync(path.dirname(legacyAbsolute),{recursive:true});
    fs.writeFileSync(sourcePdf,'original PDF bytes');
    const anchor={id:'hl-old',kind:'pdf',page:1,exact:'原文',color:'blue'};
    const logLine=JSON.stringify({reset:true,at:Date.now()})+'\n'+JSON.stringify({filePath:'讲义.pdf',anchor})+'\n';
    fs.writeFileSync(absolute,logLine);
    fs.writeFileSync(legacyAbsolute,JSON.stringify({files:{'讲义.pdf':[anchor]}}));
    const resetHost=Object.create(PluginClass.prototype);
    resetHost.manifest={dir:'.obsidian/plugins/noting-helper-bridge'};
    resetHost.settings={highlightJournal:journalRel};
    resetHost.app={vault:{adapter:{getBasePath:()=>resetRoot}}};
    resetHost.highlightStore={version:2,files:{'讲义.pdf':[anchor]}};
    resetHost.pendingAnchorEdits=new Map();
    resetHost.openAnnotationCache=new Map();
    resetHost.captures=new Map();
    resetHost.hideHighlightPreview=()=>{};
    resetHost.closeHighlightMenu=()=>{};
    resetHost.refreshEditorDecorations=()=>{};
    resetHost.refreshHighlights=()=>{};
    const result=await resetHost.resetHighlights(1);
    assert.strictEqual(result.removed,1);
    assert.strictEqual(resetHost.activeHighlightCount(),0);
    assert.strictEqual(fs.readFileSync(sourcePdf,'utf8'),'original PDF bytes');
    assert.strictEqual(fs.readFileSync(path.join(resetRoot,...result.backup.split('/')),'utf8'),logLine);
    assert.deepStrictEqual(JSON.parse(fs.readFileSync(absolute,'utf8').trim()).reset,true);
    const reloaded=Object.create(PluginClass.prototype);
    reloaded.manifest=resetHost.manifest;
    reloaded.settings=resetHost.settings;
    reloaded.app={vault:{adapter:{read:async(rel)=>fs.promises.readFile(path.join(resetRoot,...rel.split('/')),'utf8'),
      exists:async(rel)=>fs.existsSync(path.join(resetRoot,...rel.split('/'))),
      list:async()=>({files:[]})}}};
    await reloaded.loadHighlights();
    assert.strictEqual(reloaded.activeHighlightCount(),0,'私有 reset 日志不能复活旧库');
  } finally { fs.rmSync(resetRoot,{recursive:true,force:true}); }

  {
  // 鉴权长轮询只传模式事件；断连/超时清理 waiter，不能悬挂旧响应。
  const {EventEmitter}=require('events');
  const modeHost=Object.create(PluginClass.prototype);
  modeHost.settings={token:'test-secret'};
  const modeResponse=()=>Object.assign(new EventEmitter(),{
    statusCode:200,headersSent:false,writableEnded:false,destroyed:false,
    writeHead(code){this.statusCode=code;this.headersSent=true},
    end(body){this.body=JSON.parse(body || '{}');this.writableEnded=true;this.emit('close')},
  });
  const bad=modeResponse();
  await modeHost.handleRequest({method:'GET',url:'/desktop-mode',headers:{authorization:'Bearer bad'}},bad);
  assert.strictEqual(bad.statusCode,401);
  const first=modeResponse();
  const pending=modeHost.handleRequest({method:'GET',url:'/desktop-mode',
    headers:{authorization:'Bearer test-secret'}},first);
  const duplicate=modeResponse();
  await modeHost.handleRequest({method:'GET',url:'/desktop-mode',
    headers:{authorization:'Bearer test-secret'}},duplicate);
  assert.strictEqual(duplicate.statusCode,409);
  assert.strictEqual(modeHost.requestDesktopMode('question'),true);
  await pending;
  assert.deepStrictEqual(first.body,{mode:'question'});
  assert.strictEqual(modeHost.desktopModeWaiter,null);
  const disconnected=modeResponse();
  const pendingClose=modeHost.waitForDesktopMode(disconnected);
  disconnected.emit('close');await pendingClose;
  assert.strictEqual(modeHost.desktopModeWaiter,null);
  assert.strictEqual(modeHost.requestDesktopMode('translate'),false);
  const timeout=modeResponse();
  const pendingTimeout=modeHost.waitForDesktopMode(timeout);
  modeHost.finishDesktopModeWait(null);await pendingTimeout;
  assert.deepStrictEqual(timeout.body,{mode:null});

  }
  console.log("Obsidian bridge tests passed");
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
