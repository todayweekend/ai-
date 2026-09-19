/* ============================================================
   星轨夜空皮肤 · 前端行为扩展（插件 example-skin）
   window.WB 是工作台给插件开的口子，见「设置 → 工作台插件 → 开发指南」
   ============================================================ */
(function () {
  if (!window.WB) return;

  var STATS = '/api/example-skin/stats';

  /* 1. 加载提示 */
  WB.toast('🌌 星轨夜空皮肤已启用\n（在任意会话里发 /skin 可以查状态）');

  /* 2. 往左侧功能栏加一个图标 */
  WB.addRailButton({
    icon: '🌌',
    title: '星轨皮肤 · 插件',
    onclick: function () {
      WB.get(STATS).then(function (d) {
        var box = document.createElement('div');
        box.innerHTML =
          '<p style="line-height:1.8;font-size:13px">这张皮肤来自插件 <code>example-skin</code>，' +
          '它演示了插件能改的几件事：</p>' +
          '<ul style="line-height:1.9;font-size:13px">' +
          '<li><b>背景板</b>：' + d.bg + '（plugin.json → ui.background）</li>' +
          '<li><b>配色 / 圆角 / 强调色</b>：ui.css 覆盖主题变量</li>' +
          '<li><b>这个按钮</b>：ui.js 用 WB.addRailButton 加进来的</li>' +
          '<li><b>回复文案</b>：main.py 的 before_send 加了「✦ 星轨」</li>' +
          '<li><b>自定义指令</b>：发 <code>/skin</code> 由后端插件直接作答</li>' +
          '<li><b>设置项</b>：设置面板里那行勾选，是插件注入的 HTML 片段</li>' +
          '</ul>' +
          '<p style="font-size:13px">已统计回复：<b>' + (d.replies || 0) + '</b> 条</p>' +
          '<p style="font-size:12px;opacity:.7">最近一条：' +
          (d.last ? (d.last.ai + ' · ' + d.last.text) : '（还没有）') + '</p>';
        WB.openPanel('星轨夜空皮肤', box);
      });
    }
  });

  /* 3. 监听每次 AI 回复 */
  WB.on('reply', function (e) {
    console.log('[example-skin] 回复', e);
  });

  /* 4. 接收后端 api.emit("reply", ...) 推来的实时事件 */
  WB.on('plugin:reply', function (e) {
    console.log('[example-skin] 服务端事件', e);
  });
})();
