const assert = require('assert');
const { JSDOM } = require('../obsidian-plugin/noting-helper-bridge/node_modules/jsdom');
const dom = new JSDOM('<!doctype html><html><body></body></html>', { pretendToBeVisual: true });
for (const name of ['window','document','navigator','MutationObserver','DOMParser','Node','HTMLElement','Window','getSelection']) {
  if (name in dom.window) globalThis[name] = dom.window[name].bind && name === 'getSelection' ? dom.window[name].bind(dom.window) : dom.window[name];
}
globalThis.requestAnimationFrame = dom.window.requestAnimationFrame.bind(dom.window);
globalThis.cancelAnimationFrame = dom.window.cancelAnimationFrame.bind(dom.window);
const Module=require('module');const load=Module._load;
const FakeTFile=class TFile {};
Module._load=function(n,p,m){if(n==='obsidian')return{MarkdownView:class{},ItemView:class{},setIcon:(el,name)=>{el.textContent=name},Notice:class{},Plugin:class{},TFile:FakeTFile,Modal:class{constructor(){this.contentEl=document.createElement('div');document.body.appendChild(this.contentEl)}open(){}close(){this.contentEl.remove()}}};return load.call(this,n,p,m)};
const plugin=require('../obsidian-plugin/noting-helper-bridge/main.js');
// 颜色自 2.4.0 起固定成 8 种：规则不再随 store 里的色值变化，也不再生成 rgb-<hex> 规则。
const {HIGHLIGHT_COLORS,HIGHLIGHT_COLOR_DEFS}=plugin._test;
const paletteHost=Object.create(plugin.prototype);
paletteHost.settings={};
paletteHost.highlightStore={files:{'old.md':[{color:'yellow'}],
  'new.md':[{color:'yellow',colorHex:'#ff8080'}]}};
paletteHost.applyHighlightPalette();
let rules=document.querySelector('#nh-highlight-palette').textContent;
assert.strictEqual(document.querySelector('#nh-highlight-palette').dataset.fixed,'1');
assert.strictEqual(HIGHLIGHT_COLORS.length,8);
for(const key of HIGHLIGHT_COLORS){
  assert.ok(rules.includes(`::highlight(nh-hl-${key})`),key);
  assert.ok(rules.includes(`.nh-highlight-swatches .nh-hl-${key} { background: ${HIGHLIGHT_COLOR_DEFS[key].hex}; }`),key);
}
// 黄仍是 #e0ac00，用户既有的色彩习惯不变。
assert.ok(rules.includes('rgba(224, 172, 0, 0.66)'));
assert.ok(!rules.includes('nh-hl-rgb-'));
// 重复调用只覆盖同一个 style 元素，不会越积越多。
paletteHost.highlightStore={files:{}};
paletteHost.applyHighlightPalette();
rules=document.querySelector('#nh-highlight-palette').textContent;
assert.strictEqual(document.querySelectorAll('#nh-highlight-palette').length,1);
assert.ok(rules.includes('::highlight(nh-hl-yellow)'));
assert.ok(!rules.includes('nh-hl-rgb-'));
const {EditorState,editorMarks,replaceEditorMarks}=plugin._test;
const {EditorView}=require('../obsidian-plugin/noting-helper-bridge/node_modules/@codemirror/view');
const state=EditorState.create({doc:'重复文本 重复文本',extensions:[editorMarks]});
const view=new EditorView({state,parent:document.body});
view.dispatch({effects:replaceEditorMarks.of([{from:5,to:9,color:'yellow'}])});
assert.strictEqual(view.state.field(editorMarks).size, 1);
assert.deepStrictEqual([...view.contentDOM.querySelectorAll('.nh-hl-yellow')].map(x=>x.textContent), ['重复文本']);
view.dispatch({changes:{from:0,insert:'前'}});
assert.deepStrictEqual([...view.contentDOM.querySelectorAll('.nh-hl-yellow')].map(x=>x.textContent), ['重复文本']);
// 模拟 PDF.js 的跨行选区：Selection.toString() 有视觉换行，文本层
// 相邻 span 却无分隔；重复出现时要捕获第二处，并映射为合法 Range。
const {pdfAnchorFromSelection,readPageText,locateInPage,slicesForRange,rangesForSlices} = plugin._test;
const pdf=document.createElement('div');
pdf.innerHTML='<div data-page-number="7"><div class="textLayer"><span>甲行</span><span>乙行。</span><span>甲行</span><span>乙行。</span></div></div>';
document.body.appendChild(pdf);
const layer=pdf.querySelector('.textLayer');
const spans=layer.querySelectorAll('span');
const pdfRange=document.createRange();
pdfRange.setStart(spans[2].firstChild,0);
pdfRange.setEnd(spans[3].firstChild,2);
const selection=dom.window.getSelection();
selection.removeAllRanges();selection.addRange(pdfRange);
// 浏览器 PDF.js 的 Selection.toString() 与 jsdom 的纯 DOM 实现不同。
pdf.ownerDocument.getSelection=()=>({rangeCount:1,isCollapsed:false,
  toString:()=> '甲行\n乙行',getRangeAt:()=>pdfRange});
const captured=pdfAnchorFromSelection(pdf);
assert.deepStrictEqual(captured,{page_number:7,exact:'甲行\n乙行',occurrence:1});
const nodes=[...layer.querySelectorAll('span')].map(x=>x.firstChild);
const page=readPageText(nodes);
const hit=locateInPage(page.text,captured.exact,captured.occurrence);
const ranges=rangesForSlices(slicesForRange(page.parts,hit.start,hit.end),()=>document.createRange());
assert.strictEqual(ranges.length,2);
assert.deepStrictEqual(ranges.map(r=>r.toString()),['甲行','乙行']);
const pdfHost=Object.create(plugin.prototype);
pdfHost.anchorsForFile=()=>[{kind:'pdf',page:7,exact:captured.exact,
  occurrence:captured.occurrence,color:'yellow'}];
const byColor=new Map(), notes=[];
assert.strictEqual(pdfHost.collectPdfRanges(pdf,'资料/例子.pdf',byColor,notes),false);
assert.deepStrictEqual([...byColor.get('nh-hl-yellow')].map(r=>r.toString()),
  ['甲行','乙行']);
assert.ok(notes.some(n=>n.includes('上色 2 段')));
// body 上的无关 UI 改动不能触发全 PDF/阅读视图重画；文本层变化与
// 分区整体替换则必须触发，保留 PDF.js 虚拟化时的自愈路径。
const {mutationsTouchHighlightRoots}=plugin._test;
const unrelated=document.createElement('aside');document.body.appendChild(unrelated);
const uiChange=document.createElement('button');unrelated.appendChild(uiChange);
assert.strictEqual(mutationsTouchHighlightRoots([
  {target:unrelated,addedNodes:[uiChange],removedNodes:[]}
],[pdf]),false);
assert.strictEqual(mutationsTouchHighlightRoots([
  {target:layer,addedNodes:[spans[0]],removedNodes:[]}
],[pdf]),true);
const replacement=document.createElement('section');replacement.appendChild(pdf);
assert.strictEqual(mutationsTouchHighlightRoots([
  {target:document.body,addedNodes:[replacement],removedNodes:[]}
],[pdf]),true);
// 真实 CodeMirror 事务：同文重复片段的末字删除后，重画与 JSONL 重载都不能丢色。
const {EditorView: CMView}=require('../obsidian-plugin/noting-helper-bridge/node_modules/@codemirror/view');
const filePath='重复.md';
const id='hl-test';
const base='重复文本 重复文本';
const initial={id,kind:'md',start:5,end:9,exact:'重复文本',prefix:'重复文本 ',suffix:'',color:'yellow'};
const journal=new Map([['.obsidian/plugins/noting-helper-bridge/storage/highlights.jsonl',
  JSON.stringify({reset:true,at:Date.now()})+'\n'+JSON.stringify({filePath,anchor:initial})+'\n']]);
const adapter={
  read:async key=>{if(!journal.has(key))throw new Error('ENOENT');return journal.get(key)},
  write:async(key,text)=>journal.set(key,text),mkdir:async()=>{},
  exists:async key=>key.endsWith('highlight-journal')||journal.has(key),
  list:async dir=>({files:[...journal.keys()].filter(key=>key.startsWith(dir+'/'))}),
};
const host=Object.create(plugin.prototype);
host.manifest={dir:'.obsidian/plugins/noting-helper-bridge'};
host.settings={highlightJournal:'.obsidian/plugins/noting-helper-bridge/storage/highlights.jsonl'};
host.app={vault:{adapter,getName:()=>"测试仓库"}};
host.highlightStore={version:2,files:{[filePath]:[{...initial}]}};
host.pendingAnchorEdits=new Map();
let tracked;
host.markdownContentRoots=()=>[{filePath,cm:tracked}];
tracked=new CMView({state:EditorState.create({doc:base,extensions:[editorMarks,
  CMView.updateListener.of(update=>host.handleEditorUpdate(update))]}),parent:document.body});
tracked.dispatch({effects:replaceEditorMarks.of([{from:5,to:9,color:'yellow',id}])});
async function flushAndReload(){
  await host.flushAnchorEdits();
  const fresh=Object.create(plugin.prototype);
  fresh.manifest=host.manifest;fresh.app=host.app;fresh.settings=host.settings;
  await fresh.loadHighlights();
  return fresh.anchorsForFile(filePath);
}
(async()=>{
  // Native entry captures the same exact source selection before a palette click,
  // and delegates to the existing write-before-paint operation without transport.
  const native=Object.create(plugin.prototype);
  const nativeFile=Object.assign(new FakeTFile(),{path:'native.md',extension:'md'});
  const nativeView={file:nativeFile,containerEl:tracked.dom,getMode:()=> 'source',
    editor:{cm:tracked,getSelection:()=> '重复文本',posToOffset:pos=>pos,
      getCursor:side=>side==='from'?5:9}};
  native.app={workspace:{activeLeaf:{view:nativeView},getActiveFile:()=>nativeFile,
    getActiveViewOfType:()=>nativeView},vault:{getName:()=> '测试仓库'}};
  native.settings={nativeHighlightEntry:true,highlightColors:{yellow:'#aa3300',green:'#126b25',blue:'#124d9f'}};
  native.captures=new Map();
  native.purgeExpiredCaptures=()=>{};
  native.onNativeSelectionEnd({button:0,target:tracked.contentDOM,clientX:90,clientY:45});
  assert.strictEqual(document.querySelectorAll('.nh-native-swatch').length,3,
    '悬浮窗只摆配置的 3 种，不是全部 8 种');
  // 没配 nativeHighlightColors 就走默认那 3 个，第一个是黄（复现 2.4.0 之前的观感）。
  assert.strictEqual(document.querySelector('.nh-native-swatch').title,'添加黄高光');
  const capturedId=native.nativeCaptureId;
  assert.deepStrictEqual({selection:native.captures.get(capturedId).selection,
    start:native.captures.get(capturedId).startOffset},
    {selection:'重复文本',start:5});
  let releaseNative;
  const nativeWritten=new Promise(resolve=>{releaseNative=resolve});
  let nativeCalls=0;
  native.applyHighlight=async(payload,response)=>{
    nativeCalls++;
    assert.strictEqual(payload.capture_id,capturedId);
    assert.strictEqual(payload.color,'yellow');
    await nativeWritten;
    response.writeHead(200);response.end(JSON.stringify({highlight_id:'hl-native'}));
  };
  native.nativePalette.querySelector('button').click();
  assert.strictEqual(nativeCalls,1);
  assert.ok(native.nativePalette?.classList.contains('is-saving'));
  native.nativePalette.querySelector('button').click();
  assert.strictEqual(nativeCalls,1,'保存中不重复发起');
  releaseNative();
  await new Promise(resolve=>setImmediate(resolve));
  assert.strictEqual(native.nativePalette,null,'成功后收起色盘');
  native.onNativeSelectionEnd({button:0,target:tracked.contentDOM,clientX:90,clientY:45});
  const modeButtons=[...native.nativePalette.querySelectorAll('.nh-native-modes .nh-native-mode')];
  assert.strictEqual(modeButtons.length,3,'色盘下方紧贴三个模式图标');
  assert.strictEqual(modeButtons[1].getAttribute('aria-pressed'),'true');
  assert.ok(modeButtons[2].querySelector('.nh-word-mode-icon'),'单词图标使用 feather.png');
  assert.ok(native.nativePalette.querySelector('.nh-native-sidebar'),'色盘可直接打开侧栏');
  let requestedMode=null;
  native.requestDesktopMode=mode=>{requestedMode=mode;return true};
  modeButtons[0].click();
  assert.strictEqual(requestedMode,'question');
  assert.strictEqual(native.nativePalette,null,'切换后原生色盘应收起');
  native.onNativeSelectionEnd({button:0,target:tracked.contentDOM,clientX:90,clientY:45});
  assert.strictEqual(native.nativePalette,null,'桌面接管期间不能再次弹出原生入口');
  // 侧栏只读取活动锚点；卡片展示、就地编辑与跳转都不写源文档。
  const addedStamp=Date.parse('2024-03-05T08:09:00');
  const changedStamp=Date.parse('2024-03-06T21:05:00');
  const {highlightTimeLabel}=plugin._test;
  assert.match(highlightTimeLabel(addedStamp),/^2024-03-05 \d{2}:\d{2}$/);
  let jumped='';
  const sidebarPatch={};
  const sidebarHost=Object.assign(Object.create(plugin.prototype),{
    highlightStore:{files:{'源/a.md':[
      {id:'one',kind:'md',exact:'<script>原文',note:'解释 [图片:图.png]',tags:['#练习'],
        title:'例句要点',color:'yellow',createdAt:addedStamp,updatedAt:changedStamp},
      {id:'two',kind:'md',exact:'第二段',color:'green',createdAt:addedStamp}],
      '源/b.pdf':[{id:'three',kind:'pdf',exact:'跨行',page:7,color:'blue',createdAt:addedStamp,
        note:'图注',noteLayout:'看图\n[图片:图.png]',images:['NoteDataOfY/图.png'],
        imageNames:['图.png']}]}},
    settings:{},
    lastHighlightSourcePath:'源/b.pdf',
    app:{workspace:{getActiveFile:()=>({path:'源/a.md'})},
      vault:{getMarkdownFiles:()=>[],getAbstractFileByPath:()=>null,
        adapter:{getResourcePath:relative=>`app://vault/${relative}`}}},
    openHighlightById:async id=>{jumped=id},
    changeHighlight:async(id,patch,remove)=>{
      Object.assign(sidebarPatch,{id},patch);
      // 真机上 changeHighlight 会先把这次改动落进 store 再返回，侧栏正是靠这份数据
      // 认出哪张卡变了。桩不写 store 的话，保存看起来就像什么都没改。
      const list=sidebarHost.highlightStore.files['源/a.md'];
      const at=list.findIndex(item=>item.id===id);
      if(at<0) return;
      if(remove) list.splice(at,1);
      else list[at]={...list[at],...patch,updatedAt:changedStamp+1};
    }});
  // Color choices come from actual records in all documents, including legacy
  // color snapshots, regardless of the configured three entry colors.
  const colorHost=Object.assign(Object.create(sidebarHost),{
    settings:{nativeHighlightColors:['green','purple','gray']},
    highlightStore:{files:{
      '源/a.md':[
        {id:'cy',kind:'md',exact:'黄条',color:'yellow'},
        {id:'cm',kind:'md',exact:'品红条',color:'yellow',colorHex:'#ff00ff'},
        {kind:'md',exact:'无凭据条',color:'red'},
      ],
      '源/b.pdf':[{id:'cb',kind:'pdf',exact:'蓝条',color:'blue',page:1}],
    }},
  });
  const colorSidebar=new plugin._test.HighlightSidebarView({},colorHost);
  colorSidebar.body=document.createElement('div');document.body.appendChild(colorSidebar.body);
  colorSidebar.render();
  const colorSelect=colorSidebar.colorSelect;
  const optionKeys=()=>[...colorSelect.options].map(option=>option.value);
  const colorIds=()=>[...colorSidebar.entries.querySelectorAll('[data-highlight-id]')].map(card=>card.dataset.highlightId);
  const filterColor=key=>{colorSelect.value=key;colorSelect.dispatchEvent(new dom.window.Event('change',{bubbles:true}));};
  assert.strictEqual(colorSidebar.scopeButton.nextElementSibling,colorSelect,'颜色筛选紧接当前文档按钮');
  assert.deepStrictEqual(optionKeys(),['all','yellow','blue','magenta']);
  assert.deepStrictEqual([...colorSelect.options].map(option=>option.textContent),['全部','黄','蓝','品红']);
  assert.deepStrictEqual(colorIds(),['cy','cm']);
  colorSidebar.body.scrollTop=200;filterColor('magenta');
  assert.strictEqual(colorSidebar.body.scrollTop,0,'改变颜色筛选回到列表顶部');
  assert.deepStrictEqual(colorIds(),['cm'],'使用实际颜色快照筛选旧记录');
  assert.deepStrictEqual(optionKeys(),['all','yellow','blue','magenta'],'筛选不能缩减颜色候选');
  colorHost.settings.nativeHighlightColors=['red','orange','green'];colorSidebar.render();
  assert.deepStrictEqual(optionKeys(),['all','yellow','blue','magenta'],'配置颜色变化不影响现存颜色筛选');
  filterColor('blue');
  assert.deepStrictEqual(colorIds(),[],'当前文档范围仍然生效');
  colorSidebar.scopeButton.click();
  assert.deepStrictEqual(colorIds(),['cb'],'切换全部文档继续按颜色过滤');
  colorSidebar.query='品红';colorSidebar.searchInput.value='品红';colorSidebar.renderEntries();
  assert.deepStrictEqual(colorIds(),[],'文字搜索与颜色筛选共同生效');
  assert.deepStrictEqual(optionKeys(),['all','yellow','blue','magenta'],'搜索不改变候选颜色');
  filterColor('magenta');assert.deepStrictEqual(colorIds(),['cm']);
  assert.strictEqual(colorSidebar.revealHighlight('cb'),true,'正文跳转可以定位被筛选掉的高光');
  assert.strictEqual(colorSidebar.colorFilter,'all');assert.strictEqual(colorSelect.value,'all');
  filterColor('blue');
  colorHost.highlightStore.files['源/b.pdf']=[];colorSidebar.render();
  assert.deepStrictEqual(optionKeys(),['all','yellow','magenta']);
  assert.strictEqual(colorSidebar.colorFilter,'all','删除最后一条已选颜色记录后回到全部');
  colorHost.highlightStore.files['源/b.pdf']=[{id:'cg',kind:'pdf',exact:'绿条',color:'green'}];
  colorSidebar.render();assert.deepStrictEqual(optionKeys(),['all','yellow','green','magenta']);
  colorSidebar.entries.remove();colorSidebar.render();
  assert.deepStrictEqual([...colorSidebar.colorSelect.options].map(option=>option.value),['all','yellow','green','magenta'],
    '面板骨架重建后仍生成颜色选项');
  colorSidebar.body.remove();await colorSidebar.onClose();
  const sidebar=new plugin._test.HighlightSidebarView({},sidebarHost);
  sidebar.body=document.createElement('div');document.body.appendChild(sidebar.body);
  sidebar.render();
  const cards=()=>[...sidebar.body.querySelectorAll('.nh-sidebar-card')];
  assert.strictEqual(cards().length,2,'当前打开文件优先于上一文件缓存');
  assert.strictEqual(sidebar.body.querySelectorAll('.nh-sidebar-entries h4').length,0,
    '当前文档模式下卡片自带文件名，不再分组');
  assert.ok(!sidebar.body.querySelector('script'),'原文必须以纯文本显示');
  // 这几条锚点都没有 colorHex（旧记录），只靠正文绘制才生成底色的路径覆盖不到它们，
  // 渲染本身必须补一次；补过之后不再重建整张样式表。
  const paletteStyles=()=>[...document.querySelectorAll('#nh-highlight-palette')];
  const paletteText=paletteStyles().map(style=>style.textContent).join('\n');
  assert.ok(paletteText.includes('.theme-dark .nh-sidebar-card.nh-hl-yellow {'),
    '侧栏渲染要补上卡片底色规则');
  assert.ok(paletteText.includes('--nh-card-tint'));
  const styleCount=paletteStyles().length;
  sidebar.renderEntries();
  assert.strictEqual(paletteStyles().length,styleCount,'色值已覆盖后不再重复建样式表');
  const first=cards()[0];
  // 卡片底色直接取这条记录的高光色：class 与 ::highlight 共用 renderedColor 后缀。
  assert.ok(first.classList.contains('nh-hl-yellow'));
  assert.strictEqual(first.style.getPropertyValue('--nh-card-accent'),'#e0ac00');
  assert.deepStrictEqual([...first.children].map(el=>el.className),
    ['nh-sidebar-head','nh-sidebar-quote','nh-annotation-title','nh-sidebar-stamp','nh-sidebar-meta',
      'nh-sidebar-tags','nh-sidebar-actions','nh-sidebar-body'],
    '字段顺序：头部 → 原文框 → 注释标题 → changed_time → 文档名/页码 → 标签 → 按钮 → 注释预览框');
  // 注释标题紧贴划线原文下面，风格与原文一致（同为引文式小字），格式「注释标题：xxx」；
  // 没设标题的那条不占位。
  const titleRow=first.querySelector('.nh-annotation-title');
  assert.strictEqual(titleRow.textContent,'注释标题：例句要点');
  assert.strictEqual(titleRow.querySelector('.nh-annotation-title-label').textContent,'注释标题：');
  assert.strictEqual(cards()[1].querySelector('.nh-annotation-title'),null,
    '没设注释标题的卡片不显示这一行');
  const head=first.querySelector('.nh-sidebar-head');
  // 颜色名跟着记录走：8 色表里 yellow 的标签就是「黄」。
  assert.strictEqual(head.querySelector('.nh-sidebar-color').textContent,'黄');
  assert.deepStrictEqual([...head.querySelectorAll('.nh-sidebar-chip')].map(c=>c.textContent),
    ['markdown','highlight']);
  const stamps=[...first.querySelectorAll('.nh-sidebar-stamp')];
  assert.deepStrictEqual(stamps.map(s=>s.querySelector('.nh-sidebar-stamp-label').textContent),
    ['added_time','changed_time'],'两个时间前用 added_time / changed_time 小字标明');
  assert.strictEqual(stamps[0].lastElementChild.textContent,highlightTimeLabel(addedStamp));
  assert.strictEqual(stamps[1].lastElementChild.textContent,highlightTimeLabel(changedStamp),
    'changed_time 取该记录末次注释 patch 的 at');
  assert.ok(head.contains(stamps[0]),'added_time 在右上角');
  assert.strictEqual(cards()[1].querySelectorAll('.nh-sidebar-stamp').length,1,
    '没有注释修改的第二条不显示 changed_time');
  assert.ok(first.querySelector('.nh-sidebar-quote').textContent.includes('<script>原文'));
  const meta=first.querySelector('.nh-sidebar-meta');
  assert.strictEqual(meta.querySelector('.nh-sidebar-file').textContent,'a.md');
  assert.strictEqual(meta.querySelector('.nh-sidebar-page'),null,'Markdown 记录没有页码');
  assert.strictEqual(first.querySelector('.nh-sidebar-tags').textContent,'#练习');
  // 整卡不再是按钮：点正文不跳转，只有 ↗ 跳。
  first.querySelector('.nh-sidebar-quote').click();
  await new Promise(resolve=>setImmediate(resolve));
  assert.strictEqual(jumped,'');
  assert.strictEqual(first.getAttribute('role'),null);
  assert.deepStrictEqual([...first.querySelectorAll('.nh-sidebar-action')].map(b=>b.textContent),
    ['square-pen','external-link','file-output','trash-2'],'四个按钮用图标表示');
  assert.deepStrictEqual([...first.querySelectorAll('.nh-sidebar-action')]
    .map(b=>b.getAttribute('aria-label')),['编辑注释','跳转到原文','导出这一条','删除这条高光及注释']);
  first.querySelectorAll('.nh-sidebar-action')[1].click();
  await new Promise(resolve=>setImmediate(resolve));
  assert.strictEqual(jumped,'one');
  // 单条导出：只把这一条交给插件写进桌面端配置好的索引文档与注释目录，卡片本身不动。
  let exportedId='';
  sidebarHost.exportSingleHighlight=async value=>{exportedId=value;return{index:'问题库/索引.md'}};
  first.querySelectorAll('.nh-sidebar-action')[2].click();
  await new Promise(resolve=>setImmediate(resolve));
  assert.strictEqual(exportedId,'one','导出这一条只传这一条的 id');
  // 还没配置导出目标时插件会抛错，侧栏只能弹提示——异常不许冒泡成未捕获 rejection。
  const unhandled=[];
  const onUnhandled=reason=>unhandled.push(reason);
  process.on('unhandledRejection',onUnhandled);
  sidebarHost.exportSingleHighlight=async()=>{throw new Error('尚未配置单条导出目标')};
  first.querySelectorAll('.nh-sidebar-action')[2].click();
  await new Promise(resolve=>setTimeout(resolve,10));
  process.removeListener('unhandledRejection',onUnhandled);
  assert.deepStrictEqual(unhandled,[],'导出失败只弹提示');
  assert.ok(first.isConnected,'导出失败不影响卡片');
  // 点 ✎ 就地向下展开：编辑器进卡片，不另开弹窗；图片栏随编辑器一同出现。
  const beforeEditors=document.querySelectorAll('.nh-highlight-annotation-editor').length;
  first.querySelectorAll('.nh-sidebar-action')[0].click();
  assert.strictEqual(document.querySelectorAll('.nh-highlight-annotation-editor').length,
    beforeEditors+1);
  const inline=first.querySelector('.nh-sidebar-editor');
  assert.ok(inline,'编辑器就地在卡片内展开，而不是弹窗');
  assert.ok(first.contains(inline));
  assert.ok(inline.querySelector('.nh-highlight-editor-images'),'展开后才出现已添加图片栏');
  assert.strictEqual(inline.querySelector('.nh-highlight-note').value,'解释 [图片:图.png]');
  assert.strictEqual(sidebar.isEditing(),true);
  // 「保存/取消」不再单起一行粘在底部：挂到卡片上 ✎/↗/🗑 那一行的右端，和操作图标同一行、
  // 就在编辑框上方一步之内。
  const inlineActions=first.querySelector('.nh-sidebar-actions');
  assert.strictEqual(inline.querySelector('.nh-highlight-actions'),null,
    '侧栏编辑器不再自带保存/取消行');
  assert.deepStrictEqual([...inlineActions.querySelectorAll('.nh-sidebar-edit-action')]
    .map(button=>[button.textContent,button.className.split(' ').includes('is-primary')]),
    [['保存',true],['取消',false]],'「保存」是操作行右端的主按钮，「取消」跟在后面');
  // 注释标题框排在正文编辑框上面，单行；保存时与注释正文一起写进 patch。
  const inlineTitle=inline.querySelector('.nh-annotation-title-input');
  assert.strictEqual(inlineTitle.value,'例句要点');
  assert.ok(inlineTitle.compareDocumentPosition(inline.querySelector('.nh-highlight-editor'))&4,
    '注释标题框在正文编辑框上边');
  assert.strictEqual(inlineTitle.getAttribute('placeholder')?.includes('导出时用作注释目录名'),true,
    '占位文案说明标题会成为导出目录名');
  // 保存后视口要停在原地：侧栏的滚动容器是视图 body，卡片列表是整体替换的，
  // replaceChildren() 把容器清空的那一瞬浏览器就把 scrollTop 夹到 0——刚编辑完的那条
  // 通常在中段，跳回顶部就得重新找。jsdom 不做布局（scrollTop 恒为 0），这里用一个
  // 「清空即归零」的滚动替身把浏览器那条行为演出来，再看重建有没有把位置接回去。
  let scrollTop=0;
  Object.defineProperty(sidebar.body,'scrollTop',{
    get:()=>scrollTop,
    set:value=>{scrollTop=value;}});
  const clearEntries=sidebar.entries.replaceChildren.bind(sidebar.entries);
  sidebar.entries.replaceChildren=(...args)=>{
    clearEntries(...args);
    if(!sidebar.entries.childElementCount) scrollTop=0;
  };
  sidebar.body.scrollTop=320;
  const beforeSave=cards();
  inlineTitle.value='  例句与高分句型  ';
  inlineActions.querySelector('.nh-sidebar-edit-action.is-primary').click();
  await new Promise(resolve=>setTimeout(resolve,0));
  assert.strictEqual(sidebar.isEditing(),false);
  assert.strictEqual(sidebar.body.scrollTop,320,'保存后停在原来的滚动位置，不跳回顶部');
  // 保存只该重画被改的那一张：整块重建会把滚动位置清掉，也会让预览里的缩略图全部
  // 重新取一遍（src 是 app:// 资源，重建就重新解码，看得见闪）。
  const afterSave=cards();
  assert.strictEqual(afterSave.length,beforeSave.length);
  assert.notStrictEqual(afterSave[0],beforeSave[0],'签名变了的卡片重建');
  assert.strictEqual(afterSave[1],beforeSave[1],'没变的卡片复用同一个节点');
  // 侧栏最热的一条路径其实是「在笔记里打字」：handleEditorUpdate 末尾的 docChanged
  // 让每个按键都在 60 ms 去抖后刷一次（见 scheduleHighlightSidebarRefresh）。数据没变
  // 时一个节点都不该重建，否则打字全程都在白画整列卡片。
  const stable=cards();
  sidebar.renderEntries();
  sidebar.renderEntries();
  assert.deepStrictEqual(cards(),stable,'数据没变时刷新不重建任何卡片');
  // 反过来，「当前文档 / 全部文档」换的是整份列表，旧位置在新列表里指到别处，该回顶部。
  // 连点两次切回原状态，后面的用例还靠「当前文档」这个前提。
  sidebar.body.scrollTop=320;
  sidebar.body.querySelector('.nh-sidebar-controls button').click();
  assert.strictEqual(sidebar.body.scrollTop,0,'切换浏览范围回到顶部');
  sidebar.body.querySelector('.nh-sidebar-controls button').click();
  assert.strictEqual(sidebar.currentOnly,true,'范围切回当前文档，后续用例的前提不变');
  assert.strictEqual(sidebarPatch.note,'解释 [图片:图.png]');
  assert.strictEqual(sidebarPatch.title,'例句与高分句型','标题随注释一起保存并去掉首尾空白');
  const refreshed=sidebar.body.querySelector('[data-highlight-id="one"]');
  assert.strictEqual(refreshed.querySelector('.nh-sidebar-editor'),null,'保存后收起回预览');
  assert.strictEqual(refreshed.querySelector('.nh-sidebar-body').tagName,'DIV');
  // 没有注释的卡片也要能点 ✎（本来就没有预览框，编辑器接在按钮行之后），
  // 「取消」只收起、不写盘。
  const second=sidebar.body.querySelector('[data-highlight-id="two"]');
  second.querySelectorAll('.nh-sidebar-action')[0].click();
  assert.strictEqual(second.querySelector('.nh-sidebar-body').previousElementSibling.className,
    'nh-sidebar-actions','没有预览框时编辑器就接在按钮行下面');
  assert.strictEqual(second.querySelector('.nh-highlight-note').value,'');
  // 编辑注释时 Tab 要缩进正文，而不是把焦点交给下一个控件（textarea 的默认行为会把
  // 用户从编辑框里踢出去）。无选区插一个缩进单位，Shift+Tab 原样退回。
  const codeBox=second.querySelector('.nh-highlight-note');
  const codeTools=second.querySelector('.nh-highlight-code-tools');
  const chooseCode=label=>[...codeTools.querySelectorAll('button')].find(button=>button.textContent===label).click();
  assert.strictEqual(codeTools.querySelector('summary').textContent,'代码块');
  const code="    def run():\n\tprint('a  b')";
  codeBox.value=`前文\n\n${code}\n\n后文`;
  codeBox.setSelectionRange(4,4+code.length);
  codeBox.focus();
  const codeSummary=codeTools.querySelector('summary');
  const menuMouseDown=new dom.window.MouseEvent('mousedown',{button:0,bubbles:true,cancelable:true});
  codeSummary.dispatchEvent(menuMouseDown);
  assert.strictEqual(menuMouseDown.defaultPrevented,true,'打开代码菜单不能转移文本框焦点');
  codeSummary.click();
  assert.strictEqual(codeTools.querySelector('details').open,true);
  assert.strictEqual(document.activeElement,codeBox,'菜单浏览期间保留原生选区高光');
  assert.deepStrictEqual([codeBox.selectionStart,codeBox.selectionEnd],[4,4+code.length]);
  assert.strictEqual(codeBox.value,`前文\n\n${code}\n\n后文`,'选择语言前不修改文字');
  const pythonChoice=[...codeTools.querySelectorAll('button')].find(button=>button.textContent==='Python');
  const choiceMouseDown=new dom.window.MouseEvent('mousedown',{button:0,bubbles:true,cancelable:true});
  pythonChoice.dispatchEvent(choiceMouseDown);
  assert.strictEqual(choiceMouseDown.defaultPrevented,true,'点击语言时也要保留选区直到格式替换');
  assert.strictEqual(document.activeElement,codeBox);
  chooseCode('自动推荐');
  assert.strictEqual(codeTools.querySelector('details').open,false);
  assert.strictEqual(codeBox.selectionStart,codeBox.selectionEnd,'转换成功后收起选区高光');

  assert.strictEqual(codeBox.value,`前文\n\n\`\`\`python\n${code}\n\`\`\`\n\n后文`,
    '转换代码保持选中内容的缩进及周围正文');

  codeBox.value="  console.log('```');";
  codeBox.setSelectionRange(0,codeBox.value.length);
  chooseCode('JavaScript');
  assert.strictEqual(codeBox.value,"````javascript\n  console.log('```');\n````",
    '嵌套反引号不能提前结束代码块');
  const codePreview=document.createElement('div');
  sidebar.plugin.renderAnnotationBody(codePreview,{noteLayout:codeBox.value});
  assert.strictEqual(codePreview.querySelector('pre code').textContent,"  console.log('```');");
  assert.strictEqual(codePreview.querySelector('pre code').dataset.language,'javascript');
  sidebar.plugin.renderAnnotationBody(codePreview,{noteLayout:'```html\n  <script>bad()</script>\n```'});
  assert.strictEqual(codePreview.querySelector('script'),null,'代码预览不能执行 HTML');
  assert.strictEqual(codePreview.querySelector('code').textContent,'  <script>bad()</script>');
  codeBox.value='保留正文';codeBox.setSelectionRange(2,2);chooseCode('Python');
  assert.strictEqual(codeBox.value,'保留正文','没有选区不能插入空代码块');
  assert.deepStrictEqual([codeBox.selectionStart,codeBox.selectionEnd],[2,2]);
  assert.strictEqual(codeTools.querySelector('[role="status"]').textContent,
    '请先用鼠标选择要转换为代码块的文字。');
  codeBox.value=' \t\n';codeBox.setSelectionRange(0,codeBox.value.length);chooseCode('Python');
  assert.strictEqual(codeBox.value,' \t\n','空白选区不转换');
  assert.strictEqual(codeTools.querySelector('[role="status"]').textContent,'代码内容不能为空。');
  for (const [raw, language] of [
    ["if __name__ == '__main__':\n    run()",'python'],
    ['#include <memory>','cpp'],['CREATE TABLE users (id INT);','sql'],
    ['# 普通 Markdown 标题',''],['  {"enabled": true}','json'],
  ]) {
    codeBox.value=raw;codeBox.setSelectionRange(0,raw.length);chooseCode('自动推荐');
    assert.strictEqual(codeBox.value,`\`\`\`${language}\n${raw}\n\`\`\``,'推荐语言与记录问题一致');
  }
  let codeSaved;
  const codeHost=Object.assign(Object.create(sidebarHost),{
    changeHighlight:async(id,patch)=>{ codeSaved=patch; },
  });
  const codeEditor=codeHost.buildAnnotationEditor({id:'two',anchor:{},doc:document,onSaved:()=>{}});
  const savedCode="```python\n    if ready:\n\t    run('a  b')\n```";
  codeEditor.element.querySelector('textarea').value=savedCode;
  codeEditor.element.querySelector('.nh-highlight-actions button').click();
  await new Promise(resolve=>setTimeout(resolve,0));
  assert.strictEqual(codeSaved.note,savedCode,'保存的正文保留代码围栏及缩进');
  assert.strictEqual(codeSaved.noteLayout,savedCode,'重开编辑器时保留代码布局');
  codeEditor.dispose();
  const tabBox=second.querySelector('.nh-highlight-note');
  tabBox.value='甲\n乙';
  tabBox.setSelectionRange(1,1);
  const tabKey=new dom.window.KeyboardEvent('keydown',{key:'Tab',bubbles:true,cancelable:true});
  tabBox.dispatchEvent(tabKey);
  assert.strictEqual(tabKey.defaultPrevented,true,'Tab 必须被编辑框吃掉，不能移走焦点');
  assert.strictEqual(tabBox.value,'甲\t\n乙');
  const shiftTab=new dom.window.KeyboardEvent('keydown',{key:'Tab',shiftKey:true,bubbles:true,cancelable:true});
  tabBox.dispatchEvent(shiftTab);
  assert.strictEqual(shiftTab.defaultPrevented,true);
  assert.strictEqual(tabBox.value,'甲\n乙','Shift+Tab 退掉刚插入的那一级');
  // 选中多行时整块缩进，选区留在这一块上。
  tabBox.setSelectionRange(0,3);
  tabBox.dispatchEvent(new dom.window.KeyboardEvent('keydown',{key:'Tab',bubbles:true,cancelable:true}));
  assert.strictEqual(tabBox.value,'\t甲\n\t乙');
  assert.deepStrictEqual([tabBox.selectionStart,tabBox.selectionEnd],[0,5]);
  // Ctrl/Alt + Tab 不是缩进，留给系统与 Obsidian 自己。
  const ctrlTab=new dom.window.KeyboardEvent('keydown',{key:'Tab',ctrlKey:true,bubbles:true,cancelable:true});
  tabBox.dispatchEvent(ctrlTab);
  assert.strictEqual(ctrlTab.defaultPrevented,false);
  // 编辑框的高度上限是 20 行正文（弹窗与侧栏就地展开同值），不再是半屏。jsdom 不做布局，
  // 平时 autoGrow 因为 scrollHeight 恒为 0 早就返回了，所以这里把 textarea 的滚动尺寸与
  // 计算样式喂成假值，直接验「上限是怎么算出来的」。
  const realComputed=dom.window.getComputedStyle;
  const realInnerHeight=dom.window.innerHeight;
  const fakeMetrics=(lineHeight,scrollHeight)=>{
    dom.window.getComputedStyle=(el)=>Object.create(realComputed(el),
      {lineHeight:{value:lineHeight},fontSize:{value:'16px'}});
    Object.defineProperty(dom.window,'innerHeight',{value:900,configurable:true});
    for (const [key,value] of [['scrollHeight',scrollHeight],['offsetHeight',110],['clientHeight',100]]) {
      Object.defineProperty(tabBox,key,{value,configurable:true});
    }
  };
  const grow=()=>tabBox.dispatchEvent(new dom.window.Event('input',{bubbles:true}));
  fakeMetrics('20px',5000);
  grow();
  assert.strictEqual(tabBox.style.height,'400px','上限是 20 行 × 行高，不再按窗口比例取半屏');
  assert.strictEqual(tabBox.style.overflowY,'auto','到上限才切框内滚动');
  fakeMetrics('200px',5000);
  grow();
  assert.strictEqual(tabBox.style.height,'450px','行高很大时也不许高过半个窗口（此时取半屏）');
  fakeMetrics('20px',150);
  grow();
  assert.strictEqual(tabBox.style.height,'160px','没到上限就按内容长：scrollHeight + 上下边框');
  assert.strictEqual(tabBox.style.overflowY,'hidden');
  dom.window.getComputedStyle=realComputed;
  Object.defineProperty(dom.window,'innerHeight',{value:realInnerHeight,configurable:true});
  for (const key of ['scrollHeight','offsetHeight','clientHeight']) delete tabBox[key];
  tabBox.value='';
  sidebarPatch.note=void 0;
  second.querySelector('.nh-sidebar-actions .nh-sidebar-edit-action:not(.is-primary)').click();
  await new Promise(resolve=>setImmediate(resolve));
  assert.strictEqual(sidebarPatch.note,void 0,'取消不写盘');
  assert.strictEqual(sidebar.body.querySelector('[data-highlight-id="two"]')
    .querySelector('.nh-sidebar-editor'),null);
  const search=sidebar.body.querySelector('input');search.value='第二段';
  search.dispatchEvent(new dom.window.Event('input',{bubbles:true}));
  assert.strictEqual(cards().length,1);
  sidebar.body.querySelector('.nh-sidebar-controls button').click();
  assert.strictEqual(cards().length,1);
  assert.strictEqual(sidebar.body.querySelectorAll('.nh-sidebar-entries h4').length,1,
    '切到全部文档才按来源分组');
  sidebar.body.querySelector('input').value='';
  sidebar.body.querySelector('input').dispatchEvent(new dom.window.Event('input'));
  assert.strictEqual(cards().length,3);
  // 注释预览框复用悬停预览的排版：图片走 Vault 资源，上限恒定（min(8em,26vh)，不随内容长高）、
  // 超出部分框内滚动。这些只能靠 CSS 保证（jsdom 不做布局），就地钉住，免得日后有人把上限挪回
  // 共用的 .nh-sidebar-body 上、连编辑器一起截断。
  const stylesheet=require('fs').readFileSync(require('path')
    .join(__dirname,'../obsidian-plugin/noting-helper-bridge/styles.css'),'utf8');
  assert.match(stylesheet,/\.nh-sidebar-body \{[^}]*max-height:\s*min\(8em, 26vh\)/,
    '预览框的上限保持 min(8em, 26vh)，不随内容长高');
  assert.match(stylesheet,/\.nh-sidebar-body \{[^}]*overflow-y:\s*auto/,
    '超过上限的部分在预览框内滚动');
  // 反向定位的描边走 CSS 类而不是内联样式：深浅主题都跟 --interactive-accent 走。
  assert.match(stylesheet,/\.nh-sidebar-card\.is-revealed \{[^}]*outline:\s*2px solid var\(--interactive-accent\)/,
    '反向定位命中的卡片短暂描边');
  assert.match(stylesheet,/@keyframes nh-reveal-pulse/,'描边按时淡出，不用 JS 拆类');
  // 8 种固定颜色在样式表里各有一格，色值必须和 source.js 的 HIGHLIGHT_COLOR_DEFS 对得上：
  // 两边各写一份，就是为了让对不上时这里先炸。
  for(const key of HIGHLIGHT_COLORS){
    const hex=HIGHLIGHT_COLOR_DEFS[key].hex;
    assert.match(stylesheet,new RegExp(`\\.nh-highlight-swatches \\.nh-hl-${key} \\{ background: ${hex};`),key);
    assert.match(stylesheet,new RegExp(`::highlight\\(nh-hl-${key}\\)`),key);
    assert.match(stylesheet,new RegExp(`\\.cm-content \\.nh-hl-${key} \\{`),key);
  }
  assert.match(stylesheet,/\.nh-sidebar-editor \{[^}]*max-height:\s*none/,
    '编辑器沿用预览框的位置，但不继承它的恒定高度');
  // 编辑框高度上限 20 行：JS 侧由 autoGrow() 现算（jsdom 量不到，scrollHeight 恒为 0），
  // 这里钉住 CSS 兜底那一条，两边不许各写各的。
  assert.match(stylesheet,/\.nh-highlight-note \{[^}]*max-height:\s*min\(20lh, 50vh\)/,
    '编辑框最多长到 20 行（CSS 兜底与 autoGrow 同值）');
  assert.ok(!/\.nh-sidebar-editor \.nh-highlight-note \{[^}]*max-height/.test(stylesheet),
    '20 行上限不再由侧栏单写一份，统一走 .nh-highlight-note');
  // 注释标题那一行与 changed_time 同字号、与划线原文同一套引文样式。
  assert.match(stylesheet,/\.nh-annotation-title \{[^}]*font-size:\s*var\(--font-ui-smaller\)/,
    '注释标题与 changed_time 同字号');
  assert.match(stylesheet,/\.nh-annotation-title \{[^}]*border-left:\s*3px solid/,
    '注释标题沿用划线原文的左侧竖条');
  // 图片栏与正文框之间的分隔线画在图片栏上、且图片栏为空时整栏不显示，所以这条线的
  // 有无只取决于「有没有图片」——打开时就有的和这次刚加上的都算（jsdom 不做布局，
  // 只能这样钉住契约）。
  assert.match(stylesheet,/\.nh-highlight-editor-images \{[^}]*border-top:\s*1px solid/,
    '正文框与图片栏之间要有一条分隔线');
  assert.match(stylesheet,/\.nh-highlight-editor-images:empty \{\s*display:\s*none/,
    '没图片时图片栏连同那条分隔线一起不渲染');
  // 图片/标签多了，弹窗里的保存按钮不能被顶出可视区（粘在滚动容器底部）。
  assert.match(stylesheet,/\.modal \.nh-highlight-actions \{[^}]*position:\s*sticky/,
    '弹窗里的保存按钮粘在可视区底部');
  // 侧栏那对「保存/取消」不必再粘底：它挪到 ✎/↗/🗑 那一行的右端了。按钮用卡片自己的
  // 高光色（--nh-card-accent）上色，「保存」实色主按钮、字色取对比色。
  assert.ok(!/\.nh-sidebar-editor \.nh-highlight-actions \{[^}]*position:\s*sticky/.test(stylesheet),
    '侧栏编辑器不再自带一行粘底的保存/取消');
  assert.match(stylesheet,/\.nh-sidebar-actions \.nh-sidebar-edit-action \{[^}]*border:\s*1px solid var\(--nh-card-accent/,
    '同行按钮用卡片自己的高光色描边');
  assert.match(stylesheet,/\.nh-sidebar-edit-action\.is-primary \{[^}]*background:\s*var\(--nh-card-accent/,
    '「保存」是实色主按钮');
  assert.match(stylesheet,/\.nh-sidebar-edit-action\.is-primary \{[^}]*margin-left:\s*auto/,
    '这两个按钮被推到按钮行的右端');
  const pdfCard=sidebar.body.querySelector('[data-highlight-id="three"]');
  assert.strictEqual(pdfCard.querySelector('.nh-sidebar-page').textContent,'p.7',
    'PDF 记录才显示页码');
  assert.strictEqual(pdfCard.querySelector('.nh-sidebar-file').textContent,'b.pdf');
  const previewBox=pdfCard.querySelector('.nh-sidebar-body');
  assert.strictEqual(decodeURIComponent(previewBox.querySelector('img').src),
    'app://vault/NoteDataOfY/图.png');
  assert.strictEqual(previewBox.querySelector('img').alt,'图.png');
  assert.strictEqual(typeof previewBox.onwheel,'function',
    '侧栏预览框上限不变、但超出部分框内滚动，滚轮落在框上先滚框（和悬停预览同一个处理器，'
    + '框里没超出上限时它会直接放行、继续滚侧栏）');
  // 删掉最后一张注释图片：图片栏必须立刻空掉。曾经「空签名」与「未渲染」的哨兵值
  // 撞车，删除只把正文里的占位行去掉了，缩略图留在栏里像是删不掉，重开编辑器才消失。
  const pdfEditable=sidebar.body.querySelector('[data-highlight-id="three"]');
  pdfEditable.querySelectorAll('.nh-sidebar-action')[0].click();
  const pdfEditor=pdfEditable.querySelector('.nh-sidebar-editor');
  assert.strictEqual(pdfEditor.querySelectorAll('.nh-highlight-editor-images img').length,1);
  pdfEditor.querySelector('.nh-highlight-image-remove').click();
  assert.strictEqual(pdfEditor.querySelectorAll('.nh-highlight-editor-images img').length,0,
    '删掉最后一张图片后图片栏立刻清空');
  assert.strictEqual(pdfEditor.querySelector('.nh-highlight-editor-images').children.length,0,
    '图片栏清空到连子节点都不剩（`:empty` 才成立，那条分隔线随最后一张图一起消失）；'
    + '反过来第一张图进栏时线也就跟着出现，不依赖重开编辑器');
  assert.ok(!pdfEditor.querySelector('.nh-highlight-note').value.includes('[图片:图.png]'),
    '同时去掉正文里的占位行');
  // 再点一次 ✎ 收起：回到预览态，而不是停在那个图片 URL 已被回收的编辑器上。
  pdfEditable.querySelectorAll('.nh-sidebar-action')[0].click();
  const pdfCollapsed=sidebar.body.querySelector('[data-highlight-id="three"]');
  assert.strictEqual(pdfCollapsed.querySelector('.nh-sidebar-editor'),null,'再点 ✎ 收回预览框');
  assert.strictEqual(pdfCollapsed.querySelector('.nh-sidebar-body').className,'nh-sidebar-body');
  // 搜索要能按文档名搜，且不受「当前文档」范围限制：输入别的文件名也能搜到那份文档。
  sidebar.body.querySelector('.nh-sidebar-controls button').click();
  assert.strictEqual(cards().length,2,'切回当前文档模式');
  const byName=sidebar.body.querySelector('input');
  byName.value='b.pdf';
  byName.dispatchEvent(new dom.window.Event('input',{bubbles:true}));
  assert.deepStrictEqual(cards().map(card=>card.getAttribute('data-highlight-id')),['three'],
    '按文档名搜索要跨文档，不能只在当前文档里找');
  assert.strictEqual(sidebar.body.querySelectorAll('.nh-sidebar-entries h4').length,1,
    '命中的是别的文档，需要分组标题区分来源');
  byName.value='';
  byName.dispatchEvent(new dom.window.Event('input',{bubbles:true}));
  assert.strictEqual(cards().length,2,'清空关键词回到当前文档范围');
  sidebarHost.highlightStore.files['源/b.pdf']=[];
  sidebar.renderEntries();
  assert.strictEqual(cards().length,2);
  // 正在编辑的那条被删掉时，展开状态必须跟着作废：编辑器元素随着卡片一起脱离文档，
  // 再没人去改 expandedId，isEditing() 就恒为真，60 ms 的后台刷新再也不进这个视图
  // （见 scheduleHighlightSidebarRefresh），侧栏停在旧数据上。
  const doomed=cards()[1];
  assert.strictEqual(doomed.getAttribute('data-highlight-id'),'two');
  doomed.querySelectorAll('.nh-sidebar-action')[0].click();
  assert.strictEqual(sidebar.isEditing(),true);
  dom.window.confirm=()=>true;
  doomed.querySelectorAll('.nh-sidebar-action')[3].click();
  await new Promise(resolve=>setTimeout(resolve,0));
  assert.strictEqual(sidebar.isEditing(),false,'被删的那条不再算作「正在编辑」');
  assert.strictEqual(sidebar.body.querySelector('[data-highlight-id="two"]'),null,'卡片随之消失');
  assert.strictEqual(cards().length,1);
  sidebar.body.remove();
  // 反向定位：从正文点「跳转侧栏」后，侧栏要滚到那张卡并短暂描边。
  const revealHost=Object.assign(Object.create(plugin.prototype),{
    highlightStore:{files:{'源/a.md':[{id:'one',kind:'md',exact:'第一条',color:'yellow'}],
      '源/b.pdf':[{id:'three',kind:'pdf',exact:'跨行',page:7,color:'blue'}]}},
    settings:{},lastHighlightSourcePath:'源/b.pdf',
    app:{workspace:{getActiveFile:()=>({path:'源/a.md'})},
      vault:{getMarkdownFiles:()=>[],getAbstractFileByPath:()=>null,
        adapter:{getResourcePath:relative=>`app://vault/${relative}`}}}});
  const revealView=new plugin._test.HighlightSidebarView({},revealHost);
  revealView.body=document.createElement('div');document.body.appendChild(revealView.body);
  revealView.render();
  const revealCards=()=>[...revealView.body.querySelectorAll('.nh-sidebar-card')];
  // 定位会重画卡片，节点是新的，所以桩要打在原型上而不是某一个节点上。
  // jsdom 没有布局，scrollIntoView 恰好也是个空实现——不拦它，就断言不到「滚到哪」。
  const nativeScrollIntoView=dom.window.Element.prototype.scrollIntoView;
  let scrolled=null;
  dom.window.Element.prototype.scrollIntoView=function(options){
    scrolled={id:this.getAttribute?.('data-highlight-id'),options};
  };
  // 搜索词会把目标卡挡在 DOM 之外，定位前必须清掉，同时输入框也要跟着空。
  revealView.searchInput=document.createElement('input');
  revealView.searchInput.value='查不到的词';
  revealView.query='查不到的词';
  revealView.renderEntries();
  assert.strictEqual(revealCards().length,0,'搜索词挡掉全部卡片');
  assert.strictEqual(revealView.revealHighlight('one'),true);
  assert.strictEqual(revealView.query,'','定位前清掉搜索词');
  assert.strictEqual(revealView.searchInput.value,'');
  assert.deepStrictEqual(scrolled,{id:'one',options:{block:'center'}});
  assert.ok(revealCards()[0].classList.contains('is-revealed'),'命中后短暂描边');
  assert.strictEqual(revealView.revealHighlight('不存在'),false,'找不到就如实返回 false');
  // 「只看当前文档」只挡住别的文档：点本文的高光不该被莫名切到全量列表。
  revealView.currentOnly=true;
  revealView.scopeButton=document.createElement('button');
  revealView.scopeButton.textContent='当前文档';
  revealView.renderEntries();
  assert.ok(revealView.revealHighlight('one'));
  assert.strictEqual(revealView.currentOnly,true,'同文档时保持范围限制');
  assert.strictEqual(revealView.scopeButton.textContent,'当前文档');
  assert.ok(revealView.revealHighlight('three'));
  assert.strictEqual(revealView.currentOnly,false,'跨文档时自动放开');
  assert.strictEqual(revealView.scopeButton.textContent,'全部文档');
  revealView.onClose();
  revealView.body.remove();
  dom.window.Element.prototype.scrollIntoView=nativeScrollIntoView;
  // 侧栏未打开时先自动打开，再等视图真的能定位——onOpen 与 setViewState 的完成
  // 次序没有保证，视图可能是后来才挂上 revealHighlight 的。
  const revealSideHost=Object.create(plugin.prototype);
  let lateView=null,revealLeaves=[],openCalls=0;
  const lateLeaf={setViewState:async()=>{},view:null};
  revealSideHost.app={workspace:{
    getLeavesOfType:()=>revealLeaves,
    getRightLeaf:()=>({setViewState:async()=>{openCalls++;revealLeaves.push(lateLeaf)}}),
    revealLeaf:async()=>{}}};
  setTimeout(()=>{lateLeaf.view={revealHighlight:id=>{lateView=id;return true}}},30);
  await revealSideHost.revealHighlightInSidebar('one');
  assert.strictEqual(openCalls,1,'侧栏没开就自动打开');
  assert.strictEqual(lateView,'one','等视图就绪后再定位');
  // 一直不就绪要如实超时，不能静默当成功。
  const stuckHost=Object.create(plugin.prototype);
  stuckHost.app={workspace:{getLeavesOfType:()=>[],getRightLeaf:()=>({setViewState:async()=>{}}),
    revealLeaf:async()=>{}}};
  await assert.rejects(()=>stuckHost.revealHighlightInSidebar('one'),/未能及时打开/);
  // Sidebar goes to the right split and reuses its own leaf on repeated clicks.
  const sideHost=Object.create(plugin.prototype);
  let sidebarLeaf=null, rightRequests=0, reveals=0;
  const claudian={state:{type:'claudian-view'}};
  const rightLeaf={setViewState:async state=>{sidebarLeaf=rightLeaf;rightLeaf.state=state},view:{render(){}}};
  sideHost.app={workspace:{getLeavesOfType:()=>sidebarLeaf?[sidebarLeaf]:[],
    getRightLeaf:()=>{rightRequests++;return rightLeaf},revealLeaf:async leaf=>{assert.strictEqual(leaf,rightLeaf);reveals++}}};
  await sideHost.openHighlightSidebar();await sideHost.openHighlightSidebar();
  assert.strictEqual(rightRequests,1);assert.strictEqual(reveals,2);
  assert.strictEqual(claudian.state.type,'claudian-view');
  assert.strictEqual(rightLeaf.state.type,'noting-helper-highlights');
  // Slow configuration must block selection until the final queued state commits.
  const configHost=Object.create(plugin.prototype);
  configHost.settings={nativeHighlightEntry:true};
  let finishConfig;
  configHost.saveData=()=>new Promise(resolve=>{finishConfig=resolve});
  const disabling=configHost.configureNativeHighlightEntry(false);
  await Promise.resolve();await Promise.resolve();
  assert.strictEqual(configHost.nativeEntryTransitionCount,1);
  configHost.onNativeSelectionEnd({button:0});
  finishConfig();await disabling;
  assert.strictEqual(configHost.settings.nativeHighlightEntry,false);
  assert.strictEqual(configHost.nativeEntryTransitionCount,0);
  native.settings.nativeHighlightEntry=false;
  native.onNativeSelectionEnd({button:0,target:tracked.contentDOM,clientX:90,clientY:45});
  assert.strictEqual(native.nativePalette,null,'关闭后不再显示');
  native.settings.nativeHighlightEntry=true;
  const nativePdf=Object.assign(new FakeTFile(),{path:'native.pdf',extension:'pdf'});
  native.app.workspace.getActiveFile=()=>nativePdf;
  native.app.workspace.activeLeaf.view={file:nativePdf,containerEl:pdf};
  document.body.appendChild(pdf);
  const nativeRange=document.createRange();
  nativeRange.setStart(spans[2].firstChild,0);
  nativeRange.setEnd(spans[3].firstChild,2);
  pdf.ownerDocument.getSelection=()=>({rangeCount:1,isCollapsed:false,
    toString:()=> '甲行\n乙行',getRangeAt:()=>nativeRange});
  const nativePdfCapture=native.captureCurrentSelection();
  assert.strictEqual(nativePdfCapture.kind,'pdf');
  assert.strictEqual(native.captures.get(nativePdfCapture.capture_id).pageNumber,7);
  assert.strictEqual(native.captures.get(nativePdfCapture.capture_id).occurrence,1);
  // Plugin setting is persisted before the UI claims it is active.
  let persisted=null;
  native.saveData=async value=>{persisted={...value}};
  assert.strictEqual(await native.configureNativeHighlightEntry(false),false);
  assert.strictEqual(persisted.nativeHighlightEntry,false);
  const probeView=new EditorView({state:EditorState.create({
    doc:'甲乙丙丁',extensions:[editorMarks]}),parent:document.body});
  const probeHost=Object.create(plugin.prototype);
  probeHost.markdownContentRoots=()=>[{filePath:'probe.md',cm:probeView}];
  const probe=probeHost.armPaintProbe('hl-probe');
  const editorTrace={};
  probeHost.pushEditorAnchor('probe.md',{
    id:'hl-probe',kind:'md',start:1,end:3,exact:'乙丙',color:'yellow',colorHex:'#ff8080'},editorTrace);
  assert.ok(editorTrace.editor_lookup_start_wall_ms<=editorTrace.editor_dispatch_start_wall_ms);
  assert.ok(editorTrace.editor_dispatch_start_wall_ms<=editorTrace.editor_dispatch_end_wall_ms);
  assert.ok(editorTrace.editor_dispatch_end_wall_ms<=editorTrace.editor_lookup_end_wall_ms);
  const painted=await probeHost.finishPaintProbe(probe);
  assert.strictEqual(painted.visual_confirmed,true,'CodeMirror 下一帧必须有真实装饰节点');
  // #ff8080 不在 8 色表里，按 color 键兜到 yellow。
  assert.strictEqual(probeView.contentDOM.querySelector('.nh-hl-yellow').textContent,'乙丙');
  probeView.destroy();
  tracked.dispatch({changes:{from:8,to:9}});
  assert.strictEqual(host.anchorsForFile(filePath)[0].exact,'重复文');
  assert.strictEqual(host.anchorsForFile(filePath)[0].start,5);
  assert.strictEqual((await flushAndReload())[0].exact,'重复文');
  const paints=plugin._test.editorMarksForDocument(tracked.state.doc.toString(),host.anchorsForFile(filePath));
  tracked.dispatch({effects:replaceEditorMarks.of(paints)});
  assert.deepStrictEqual([...tracked.contentDOM.querySelectorAll('.nh-hl-yellow')].map(e=>e.textContent),['重复文']);
  assert.strictEqual(tracked.contentDOM.querySelector('[data-nh-highlight-id]').getAttribute('data-nh-highlight-id'),id);
  tracked.dispatch({changes:{from:0,insert:'前'}});
  assert.strictEqual(host.anchorsForFile(filePath)[0].start,6);
  assert.strictEqual((await flushAndReload())[0].start,6);
  tracked.dispatch({changes:{from:7,insert:'新'}});
  assert.strictEqual(host.anchorsForFile(filePath)[0].exact,'重新复文');
  assert.strictEqual((await flushAndReload())[0].exact,host.anchorsForFile(filePath)[0].exact);
  await host.changeHighlight(id,{note:'注释内容',tags:['#语文','#复习']});
  assert.strictEqual(host.anchorsForFile(filePath)[0].note,'注释内容');
  // 侧栏要显示的两个时间都不是存储字段，靠重放派生：anchor 记录里没有 createdAt，
  // 用首次出现那条记录的 at 补；注释 patch 把 updatedAt 推到该条的 at。
  assert.ok(host.anchorsForFile(filePath)[0].updatedAt>0,'保存注释要就地刷新修改时间');
  const replayed=(await flushAndReload())[0];
  assert.deepStrictEqual(replayed.tags,['#语文','#复习']);
  const journalLines=journal.get(host.settings.highlightJournal).trim().split('\n');
  const records=journalLines.map(line=>JSON.parse(line));
  // 这条记录是编辑器漂移路径新建的，anchor 里没有 createdAt，所以要退回到「首次带
  // 这条 anchor 的记录」的 at。
  const anchorStamps=records.filter(record=>record.anchor||record.updates)
    .map(record=>record.at).filter(stamp=>stamp>0);
  const lastNoteAt=Math.max(...records
    .filter(record=>record.patches?.some(patch=>patch.note!==void 0)).map(record=>record.at));
  assert.strictEqual(replayed.createdAt,Math.min(...anchorStamps),'记录时间取首次出现的记录');
  assert.strictEqual(replayed.updatedAt,lastNoteAt,'修改时间取最后一次注释 patch');
  assert.ok(replayed.updatedAt>replayed.createdAt);
  // 注释标题是注释自己的字段：改标题同样算「修改注释」（changed_time 跟着走），
  // 并且跟着 anchor 一起从 JSONL 重放回来，不需要任何迁移。
  await host.changeHighlight(id,{title:'混排标题'});
  assert.strictEqual(host.anchorsForFile(filePath)[0].title,'混排标题');
  const titleAt=Math.max(...journal.get(host.settings.highlightJournal).trim()
    .split('\n').map(line=>JSON.parse(line))
    .filter(record=>record.patches?.some(patch=>patch.title!==void 0)).map(record=>record.at));
  assert.strictEqual(host.anchorsForFile(filePath)[0].updatedAt,titleAt,'改注释标题也算修改注释');
  // anchor 自带 createdAt 时必须原样保留（真实新建路径写的就是它），改颜色不算
  // 修改注释，因此没有注释 patch 时 updatedAt 保持缺失、卡片上那一行整块不显示。
  const ownCreated=Date.parse('2024-05-06T07:08:00');
  const ownJournal=new Map([[host.settings.highlightJournal,
    `${JSON.stringify({reset:true,at:1})}\n`
    +`${JSON.stringify({filePath,anchor:{...initial,createdAt:ownCreated},at:ownCreated+5})}\n`
    +`${JSON.stringify({filePath,patches:[{id,color:'blue'}],at:ownCreated+9})}\n`]]);
  const ownHost=Object.create(plugin.prototype);
  ownHost.manifest=host.manifest;ownHost.settings=host.settings;
  ownHost.app={vault:{adapter:Object.assign({},adapter,
    {read:async key=>{if(!ownJournal.has(key))throw new Error('ENOENT');return ownJournal.get(key)},
      write:async(key,text)=>ownJournal.set(key,text)}),getName:()=>"测试仓库"}};
  await ownHost.loadHighlights();
  const ownAnchor=ownHost.anchorsForFile(filePath)[0];
  assert.strictEqual(ownAnchor.createdAt,ownCreated,'anchor 自带的记录时间优先于记录级 at');
  assert.strictEqual(ownAnchor.updatedAt,void 0,'改颜色不算修改注释');
  let clicked='';host.openHighlightMenu=highlightId=>{clicked=highlightId};
  const selected=pdf.ownerDocument.getSelection;
  pdf.ownerDocument.getSelection=()=>({toString:()=>''});
  host.onHighlightClick({button:0,target:tracked.contentDOM.querySelector('[data-nh-highlight-id]'),
    clientX:0,clientY:0,defaultPrevented:false});
  assert.strictEqual(clicked,id);
  host.app.vault.getMarkdownFiles=()=>[{path:'知识.md'}];
  host.app.metadataCache={getFileCache:()=>({tags:[{tag:'#语文'},{tag:'#英语'}]})};
  plugin.prototype.openHighlightActions.call(host,id);
  const modalBox=document.querySelector('.nh-highlight-actions').parentElement;
  // 点高光弹出的编辑器与侧栏就地展开的是同一份实现：标题框同样在正文编辑框上边。
  const modalTitle=modalBox.querySelector('.nh-annotation-title-input');
  assert.strictEqual(modalTitle?.value,'混排标题','弹窗里也要带上注释标题栏');
  assert.ok(modalTitle.compareDocumentPosition(modalBox.querySelector('.nh-highlight-editor'))&4);
  const tagInput=modalBox.querySelector('.nh-highlight-tags');
  const pool=modalBox.querySelector('.nh-highlight-tag-pool select');
  const addFromPool=modalBox.querySelector('.nh-highlight-tag-pool button');
  const originalTags=tagInput.value;
  pool.value='#英语';pool.dispatchEvent(new dom.window.Event('change',{bubbles:true}));
  assert.strictEqual(tagInput.value,originalTags,'选择池标签不应自动写入');
  tagInput.setSelectionRange(tagInput.value.length,tagInput.value.length);
  addFromPool.click();
  assert.ok(tagInput.value.includes('#英语'));
  assert.ok(modalBox.querySelector('.nh-highlight-tag-chips').textContent.includes('#英语'));
  tagInput.value='#';tagInput.setSelectionRange(1,1);
  tagInput.dispatchEvent(new dom.window.Event('input',{bubbles:true}));
  assert.strictEqual(modalBox.querySelectorAll('.nh-highlight-suggestions button').length,2);
  tagInput.dispatchEvent(new dom.window.KeyboardEvent('keydown',{key:'ArrowDown',bubbles:true}));
  tagInput.dispatchEvent(new dom.window.KeyboardEvent('keydown',{key:'Enter',bubbles:true}));
  assert.ok(tagInput.value.startsWith('#'));
  const noteInput=modalBox.querySelector('.nh-highlight-note');
  noteInput.value='更新注释';
  assert.deepStrictEqual([...modalBox.querySelectorAll('.nh-highlight-actions button')]
    .map(button=>button.textContent),['保存'],'编辑框只留保存，复制/删除在快捷菜单');
  const textPaste=new dom.window.Event('paste',{bubbles:true,cancelable:true});
  Object.defineProperty(textPaste,'clipboardData',{value:{items:[{kind:'string',type:'text/plain'}]}});
  noteInput.dispatchEvent(textPaste);
  assert.strictEqual(textPaste.defaultPrevented,false,'纯文本粘贴须保持浏览器默认行为');
  const pasted=new dom.window.File([Uint8Array.of(1,2,3,4)],'剪贴板截图.png',
    {type:'image/png'});
  const createObjectURL=URL.createObjectURL;
  URL.createObjectURL=()=> 'blob:nh-test-image'; // jsdom 不提供浏览器的 File object URL。
  const imagePaste=new dom.window.Event('paste',{bubbles:true,cancelable:true});
  Object.defineProperty(imagePaste,'clipboardData',{value:{items:[{kind:'file',type:'image/png',
    getAsFile:()=>pasted}]}});
  noteInput.setSelectionRange(noteInput.value.length,noteInput.value.length);
  noteInput.dispatchEvent(imagePaste);
  assert.strictEqual(imagePaste.defaultPrevented,true);
  assert.ok(noteInput.value.endsWith('\n[图片:剪贴板截图.png]'));
  const thumb=modalBox.querySelector('.nh-highlight-editor-images img');
  assert.ok(thumb,'图片在编辑区以缩略图呈现，而不是只显示链接');
  assert.ok(thumb.src.startsWith('blob:nh-test-image'), '剪贴板图片须有可见的图像资源');
  URL.createObjectURL=createObjectURL;
  host.storeHighlightImage=async(file)=>`NoteDataOfY/高光库/image高光数据/${file.name}`;
  modalBox.querySelector('.nh-highlight-actions button').click();
  await new Promise(resolve=>setTimeout(resolve,0));
  const saved=(await flushAndReload())[0];
  assert.strictEqual(saved.note,'更新注释','占位行不重复写入注释正文');
  assert.deepStrictEqual(saved.imageNames,['剪贴板截图.png']);
  assert.strictEqual(saved.images.length,1);
  host.app.vault.getAbstractFileByPath=relative=>({path:relative});
  host.app.vault.getResourcePath=file=>`app://vault/${file.path}`;
  plugin.prototype.openHighlightActions.call(host,id);
  const reopened=document.querySelector('.nh-highlight-actions').parentElement;
  assert.ok(reopened.querySelector('.nh-highlight-editor-images img')?.src.startsWith('app://vault/'),
    '已有图片应以 Vault 资源缩略图显示');
  const reopenedNote=reopened.querySelector('.nh-highlight-note');
  assert.ok(reopenedNote.value.includes('[图片:剪贴板截图.png]'),'重新编辑仍展示图片名称');
  const fileInput=reopened.querySelector('input[type=file]');
  Object.defineProperty(fileInput,'files',{configurable:true,value:[pasted]});
  fileInput.dispatchEvent(new dom.window.Event('change'));
  assert.ok(reopenedNote.value.includes('[图片:剪贴板截图.png (2)]'),
    '文件选择器也在编辑框显示图片行；重名要可区分');
  reopenedNote.value='更新注释'; // 删除占位行即移除图片关联，不删历史文件。
  reopened.querySelector('.nh-highlight-actions button').click();
  await new Promise(resolve=>setTimeout(resolve,0));
  assert.deepStrictEqual((await flushAndReload())[0].images,[]);
  const fs=require('fs'), os=require('os'), path=require('path');
  const temp=fs.mkdtempSync(path.join(os.tmpdir(),'nh-image-test-'));
  try {
    const imageHost=Object.create(plugin.prototype);
    imageHost.settings=host.settings;
    imageHost.manifest=host.manifest;
    imageHost.app={vault:{adapter:{getBasePath:()=>temp}}};
    const image=await imageHost.storeHighlightImage({type:'image/png',size:4,
      arrayBuffer:async()=>Uint8Array.of(1,2,3,4).buffer});
    assert.ok(image.includes('/storage/imagehighlights/'));
    assert.deepStrictEqual([...fs.readFileSync(path.join(temp,...image.split('/')))], [1,2,3,4]);
    await host.changeHighlight(id,{images:[image]});
    const doc=host.exportHighlights('NoteDataOfY/高光库/导出');
    // 注释目录名取注释标题（这条锚点上面设了标题），图片落在该注释目录下的 image/。
    assert.ok(doc.images[0].target.includes('/注释/'));
    assert.ok(doc.images[0].target.endsWith(`/混排标题/image/1-${path.posix.basename(image)}`),
      `图片目标目录应随注释标题走：${doc.images[0].target}`);
    const localAdapter={
      getBasePath:()=>temp,
      exists:async relative=>fs.existsSync(path.join(temp,...relative.split('/'))),
      read:async relative=>fs.promises.readFile(path.join(temp,...relative.split('/')),'utf8'),
      list:async()=>({files:[]}),
    };
    imageHost.app={vault:{adapter:localAdapter,getName:()=>"测试仓库"}};
    const legacyImage='NoteDataOfY/高光库/image高光数据/old.png';
    fs.mkdirSync(path.dirname(path.join(temp,...legacyImage.split('/'))),{recursive:true});
    fs.copyFileSync(path.join(temp,...image.split('/')),path.join(temp,...legacyImage.split('/')));
    imageHost.highlightStore={version:2,files:{[filePath]:[{...host.findHighlight(id).anchor,images:[legacyImage]}]}};
    imageHost.pendingAnchorEdits=new Map();
    imageHost.saveData=async()=>{};
    imageHost.settings={highlightJournal:'NoteDataOfY/高光库/高光数据.jsonl'};
    imageHost.fixedHighlightJournal='.obsidian/plugins/noting-helper-bridge/storage/highlights.jsonl';
    await imageHost.migrateHighlightStorage();
    const relocated=imageHost.anchorsForFile(filePath)[0].images[0];
    assert.ok(relocated.includes('/storage/imagehighlights/'));
    assert.ok(fs.existsSync(path.join(temp,...relocated.split('/'))));
    assert.ok(fs.existsSync(path.join(temp,...image.split('/'))),'旧图片留作备份');
    const docs=await imageHost.writeHighlightExport('NoteDataOfY/高光库/导出');
    const entry=imageHost.exportHighlights('NoteDataOfY/高光库/导出').files.find(({path})=>path.endsWith('/注释.md'));
    assert.ok(fs.readFileSync(path.join(temp,...entry.path.split('/')),'utf8').includes('image/1-'));
    assert.ok(fs.existsSync(path.join(temp,...docs.notes.split('/'))));
  } finally { fs.rmSync(temp,{recursive:true,force:true}); }
  // 两次加图、文字夹图的顺序须跨 JSONL 重放、预览与导出一致。
  host.openAnnotationCache=new Map([[filePath,new Map()]]);
  host.updateOpenAnnotation(filePath,host.findHighlight(id).anchor,id);
  let imageCounter=0;
  URL.createObjectURL=()=> 'blob:nh-test-image';
  host.storeHighlightImage=async(file)=>`NoteDataOfY/高光库/image高光数据/${++imageCounter}-${file.name}`;
  plugin.prototype.openHighlightActions.call(host,id);
  const mixed=document.querySelector('.nh-highlight-actions').parentElement;
  const mixedNote=mixed.querySelector('.nh-highlight-note');
  mixedNote.value='前文后文'; mixedNote.setSelectionRange(2,2);
  mixedNote.dispatchEvent(imagePaste);
  assert.ok(mixedNote.value.includes('前文\n[图片:剪贴板截图.png]\n后文'));
  mixedNote.setSelectionRange(mixedNote.value.length,mixedNote.value.length);
  mixedNote.dispatchEvent(imagePaste);
  assert.strictEqual(mixed.querySelectorAll('.nh-highlight-editor-images figure').length,2);
  const secondThumb=mixed.querySelectorAll('.nh-highlight-editor-images img')[1];
  secondThumb.click();
  assert.strictEqual(document.querySelector('.nh-highlight-lightbox img')?.alt,'剪贴板截图.png (2)');
  document.querySelector('.nh-highlight-lightbox').click();
  assert.strictEqual(document.querySelector('.nh-highlight-lightbox'),null);
  mixed.querySelector('.nh-highlight-actions button').click();
  await new Promise(resolve=>setTimeout(resolve,0));
  const mixedSaved=(await flushAndReload())[0];
  assert.strictEqual(mixedSaved.note,'前文\n后文');
  assert.deepStrictEqual(mixedSaved.imageNames,['剪贴板截图.png','剪贴板截图.png (2)']);
  assert.strictEqual(host.openAnnotationCache.get(filePath).get(id).noteLayout,mixedSaved.noteLayout,
    '缓存与 JSONL 成功写入后的混排内容须同步');
  assert.ok(mixedSaved.noteLayout.indexOf('前文') < mixedSaved.noteLayout.indexOf('[图片:剪贴板截图.png]'));
  assert.ok(mixedSaved.noteLayout.indexOf('[图片:剪贴板截图.png]') < mixedSaved.noteLayout.indexOf('后文'));
  assert.ok(mixedSaved.noteLayout.indexOf('后文') < mixedSaved.noteLayout.indexOf('[图片:剪贴板截图.png (2)]'));
  const ordered=host.exportHighlights('NoteDataOfY/高光库/导出').files.find(({path})=>path.endsWith('/注释.md')).text;
  assert.ok(ordered.indexOf('前文') < ordered.indexOf('1-剪贴板截图.png'));
  assert.ok(ordered.indexOf('1-剪贴板截图.png') < ordered.indexOf('后文'));
  assert.ok(ordered.indexOf('后文') < ordered.indexOf('2-剪贴板截图.png'));
  host.app.vault.getAbstractFileByPath=()=>null;
  host.app.vault.adapter.getResourcePath=relative=>`app://adapter/${relative}`;
  plugin.prototype.openHighlightActions.call(host,id);
  const again=document.querySelector('.nh-highlight-actions').parentElement;
  assert.strictEqual(again.querySelectorAll('.nh-highlight-editor-images img').length,2);
  assert.ok(again.querySelectorAll('.nh-highlight-editor-images img')[1].src.startsWith('app://adapter/'));
  pdf.ownerDocument.getSelection=()=>({toString:()=>''});
  host.onHighlightMove({target:tracked.contentDOM.querySelector('[data-nh-highlight-id]'),
    clientX:14,clientY:14});
  await new Promise(resolve=>setTimeout(resolve,145));
  const mixedPreview=document.querySelector('.nh-highlight-preview');
  const previewChildren=[...mixedPreview.children]
    .map(el=>el.tagName==='IMG' ? el.alt : el.textContent);
  // 悬停预览没有划线原文，注释标题就排在最上面（侧栏里它排在原文下面）。
  assert.deepStrictEqual(previewChildren,
    ['注释标题：混排标题','前文','剪贴板截图.png','后文','剪贴板截图.png (2)']);
  assert.strictEqual(mixedPreview.firstElementChild.className,'nh-annotation-title');
  assert.ok(mixedPreview.querySelectorAll('img')[1].src.startsWith('app://adapter/'));
  host.hideHighlightPreview(); host.hoverHighlightId=null;

  again.querySelectorAll('.nh-highlight-image-remove')[0].click();
  assert.ok(!again.querySelector('.nh-highlight-note').value.includes('[图片:剪贴板截图.png]'));
  assert.strictEqual(again.querySelectorAll('.nh-highlight-editor-images img').length,1);
  again.querySelector('.nh-highlight-actions button').click();
  await new Promise(resolve=>setTimeout(resolve,0));
  assert.deepStrictEqual((await flushAndReload())[0].imageNames,['剪贴板截图.png (2)']);
  assert.ok(host.exportHighlights('NoteDataOfY/高光库/导出').files.some(({text})=>text.includes('2-剪贴板截图.png')));
  // 仅有图片也需要索引里的“查看注释”入口。
  await host.changeHighlight(id,{note:'',noteLayout:'[图片:剪贴板截图.png (2)]'});
  assert.ok(host.exportHighlights('NoteDataOfY/高光库/导出').files.some(({text})=>text.includes('查看注释')));
  const oldAnchor={note:'旧文字',images:['a.png','b.png','c.png','d.png']};
  assert.deepStrictEqual(host.annotationParts(oldAnchor).map(part=>part.image||part.text),
    ['旧文字','a.png','b.png','c.png','d.png'],'旧记录四张图均需展示，不能只取三张');
  await host.changeHighlight(id,{note:'更新注释',noteLayout:'更新注释',images:[],imageNames:[]});
  URL.createObjectURL=createObjectURL;
  pdf.ownerDocument.getSelection=()=>({toString:()=>''});
  host.refreshEditorDecorations=()=>{};
  delete host.openHighlightMenu;
  host.onHighlightMove({target:tracked.contentDOM.querySelector('[data-nh-highlight-id]'),
    clientX:12,clientY:12});
  await new Promise(resolve=>setTimeout(resolve,145));
  const preview=document.querySelector('.nh-highlight-preview');
  assert.ok(preview?.textContent.includes('更新注释'));
  Object.defineProperty(preview,'scrollHeight',{configurable:true,value:800});
  Object.defineProperty(preview,'clientHeight',{configurable:true,value:200});
  const wheel=new dom.window.WheelEvent('wheel',{deltaY:120,bubbles:true,cancelable:true});
  preview.dispatchEvent(wheel);
  assert.strictEqual(wheel.defaultPrevented,true,'预览内容可滚动时不应让源文档跟着滚动');
  host.onHighlightMove({target:document.body,clientX:800,clientY:800});
  await new Promise(resolve=>setTimeout(resolve,200));
  assert.strictEqual(document.querySelector('.nh-highlight-preview'),null);
  host.openHighlightMenu(id,{clientX:20,clientY:20});
  const menu=document.querySelector('.nh-highlight-menu');
  assert.deepStrictEqual([...menu.children].filter(el=>el.tagName==='BUTTON').map(el=>el.textContent),
    ['编辑注释','跳转侧栏','复制链接','删除高光'],
    '菜单动作行：编辑注释 / 跳转侧栏 / 复制链接 / 删除高光（「跳转位置」已删：点高光时人就在高光处）');
  assert.strictEqual(menu.querySelectorAll('button').length,8,'四个动作 + 切换颜色 + 三个色块');
  const colorRow=document.querySelector('.nh-highlight-color-row');
  colorRow.dispatchEvent(new dom.window.Event('mouseenter'));
  assert.ok(colorRow.querySelector('.nh-highlight-swatches'));
  // 菜单里的切换颜色必须和悬浮窗同一组：不然能改成配置外的颜色，悬浮窗就再也改不回来。
  assert.deepStrictEqual([...colorRow.querySelectorAll('.nh-highlight-swatches button')]
    .map(button=>button.getAttribute('aria-label')),
    plugin._test.DEFAULT_ACTIVE_COLORS.map(key=>`切换到${HIGHLIGHT_COLOR_DEFS[key].label}`),
    '色块按序号念没意义，念颜色名；且只列配置的 3 种');
  colorRow.querySelector('.nh-hl-blue').click();
  await new Promise(resolve=>setTimeout(resolve,0));
  assert.strictEqual((await flushAndReload())[0].color,'blue');
  assert.strictEqual(document.querySelector('.nh-highlight-menu'),null);
  // 「跳转侧栏」去的是跨文档汇总的侧栏卡片，不是此刻鼠标下方的源文位置
  // （后者正是 2.3.21 删掉「跳转位置」的理由，两者不是一回事）。
  host.openHighlightMenu(id,{clientX:20,clientY:20});
  const revealMenu=document.querySelector('.nh-highlight-menu');
  let revealed='';
  host.revealHighlightInSidebar=async value=>{revealed=value};
  [...revealMenu.children].find(el=>el.tagName==='BUTTON'&&el.textContent==='跳转侧栏').click();
  await new Promise(resolve=>setTimeout(resolve,0));
  assert.strictEqual(revealed,id,'跳转侧栏把这条 id 交给视图反向定位');
  assert.strictEqual(document.querySelector('.nh-highlight-menu'),null,'跳转后收起菜单');
  // 故障时不得把内存态当作已落盘：pending 保留，可重试。
  tracked.dispatch({changes:{from:0,insert:'再'}});
  const originalAppend=host.appendHighlightRecord;
  let failedOnce=false;
  host.appendHighlightRecord=async(...args)=>{
    if(!failedOnce){failedOnce=true;throw new Error('disk unavailable')}
    return originalAppend.call(host,...args);
  };
  await assert.rejects(()=>host.flushAnchorEdits(),/disk unavailable/);
  assert.strictEqual(host.pendingAnchorEdits.size,1);
  assert.strictEqual((await flushAndReload())[0].start,7);
  host.appendHighlightRecord=originalAppend;
  const ending=host.anchorsForFile(filePath)[0];
  tracked.dispatch({changes:{from:ending.start,to:ending.end}});
  assert.strictEqual(host.anchorsForFile(filePath).length,0);
  assert.strictEqual((await flushAndReload()).length,0);
  // 全量重置必须同时清空 JSONL、缓存、Markdown 装饰及 PDF/阅读层，
  // 旧 highlights.json/分片即使保留，只要新日志有 reset 就不能复活。
  const resetMd={...initial,id:'hl-reset-md'};
  const resetPdf={id:'hl-reset-pdf',kind:'pdf',page:7,exact:'甲行乙行',color:'blue'};
  host.highlightStore.files[filePath]=[resetMd];
  host.highlightStore.files['资料/例子.pdf']=[resetPdf];
  host.refreshEditorDecorations=plugin.prototype.refreshEditorDecorations;
  host.rebuildHighlightCache();
  journal.set(`${host.manifest.dir}/highlights.json`,JSON.stringify({files:{[filePath]:[resetMd]}}));
  tracked.dispatch({effects:replaceEditorMarks.of([{from:5,to:9,color:'yellow',id:resetMd.id}])});
  assert.ok(tracked.contentDOM.querySelector('.nh-hl-yellow'));
  let paintRefreshes=0;
  host.refreshHighlights=()=>{ paintRefreshes++ };
  host.backupHighlightJournal=async()=> '测试备份.jsonl';
  host.writeSnapshot=async()=>{throw new Error('disk unavailable')};
  await assert.rejects(()=>host.resetHighlights(2),/disk unavailable/);
  assert.strictEqual(host.activeHighlightCount(),2,'写盘失败不能提前清除内存');
  assert.ok(tracked.contentDOM.querySelector('.nh-hl-yellow'),'失败不能擦除显示');
  await assert.rejects(()=>host.resetHighlights(1),/高光数量已变化/);
  host.writeSnapshot=async(target,text)=>journal.set(target,text);
  host.settings.token='test-token';
  const {Readable}=require('stream');
  const response=()=>({status:0,body:null,writeHead(code){this.status=code},
    end(text){this.body=JSON.parse(text)}});
  const request=(payload,token='test-token')=>Object.assign(
    Readable.from([JSON.stringify(payload)]),{method:'POST',url:'/highlight-reset',
      headers:{authorization:`Bearer ${token}`}});
  const unauthorized=response();
  await host.handleRequest(request({confirm:'CLEAR_ALL_HIGHLIGHTS',expected_count:2},'bad'),unauthorized);
  assert.strictEqual(unauthorized.status,401);
  const unconfirmed=response();
  await host.handleRequest(request({expected_count:2}),unconfirmed);
  assert.strictEqual(unconfirmed.status,400);
  assert.strictEqual(host.activeHighlightCount(),2);
  const success=response();
  await host.handleRequest(request({confirm:'CLEAR_ALL_HIGHLIGHTS',expected_count:2}),success);
  assert.strictEqual(success.status,200);
  const cleared=success.body;
  assert.strictEqual(cleared.removed,2);
  assert.strictEqual(host.activeHighlightCount(),0);
  assert.strictEqual(host.highlightById.size,0);
  assert.strictEqual(host.openAnnotationCache.get(filePath)?.size || 0,0);
  assert.strictEqual(tracked.contentDOM.querySelectorAll('.nh-hl-yellow').length,0);
  assert.strictEqual(paintRefreshes,1,'清空必须立刻重画 PDF/阅读视图');
  assert.strictEqual(host.lastEditorFullRefreshCount,1,'全量校准需报告编辑视图数');
  assert.ok(host.lastEditorFullRefreshMs>=0,'全量校准需报告耗时');
  assert.strictEqual((await flushAndReload()).length,0,'旧库重放后也不应复活');
  assert.deepStrictEqual(JSON.parse(journal.get(host.settings.highlightJournal).trim()).reset,true);
  host.highlightResetting=true;
  await assert.rejects(()=>host.changeHighlight(resetMd.id,{},true),/正在清空/);
  host.highlightResetting=false;
  tracked.destroy();
  console.log('CodeMirror edit persistence and PDF DOM highlight tests passed');
  view.destroy();dom.window.close();
})().catch(error=>{console.error(error);process.exitCode=1;view.destroy();tracked.destroy();dom.window.close()});
