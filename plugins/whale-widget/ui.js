/* 小鲸鱼余额挂件 · 前端（注入到工作台界面）
   多账户版：点鲸鱼切换到下一个账户；设置 → 小鲸鱼余额 里管理账户 */
(function () {
  if (window.__whaleWidgetLoaded) return;
  window.__whaleWidgetLoaded = true;

  var A = "/ext/whale-widget/assets/";
  var REFRESH_MS = 60000;
  var BAL_URL = "/api/whale-widget/balance";
  var ACC_URL = "/api/whale-widget/accounts";

  // ---- 随机台词（加权，卖萌/峰谷提示）----
  var LINES = [
    "好模型…钱包在流泪 ↓",
    "不知道用户拿我干嘛，先赶走吧~",
    "我…我…我也要挣钱吗？",
    "我去吃饭啦，测完叫我",
    "压力一只蓝色大肥鱼？！",
    "坏了…用户彻底怒了！",
    "你目录里的 dsh 是什么…大烧货吗…?",
    "恭喜你实现 token 自由！token 全跑了！",
    "真当我是便宜货啊…",
    "哦鲸鲸…",
    "今天的余额，是明天的 token～",
    "谷价时段冲！贵的不值当～",
    "点我换一个钱包看看～"
  ];

  // ---- 建 DOM ----
  var root = document.createElement("div");
  root.id = "whaleWidget";
  root.innerHTML =
    '<div class="whale-btns">' +
      '<button data-act="mute" title="静音">🔊</button>' +
      '<button data-act="refresh" title="刷新余额">⟳</button>' +
    '</div>' +
    '<img class="whale-img" src="' + A + 'DSniang1.png" alt="whale">' +
    '<div class="whale-acc"></div>' +
    '<div class="whale-bal">…</div>' +
    '<div class="whale-hint">加载中…</div>' +
    '<div class="whale-bubble"></div>';

  document.body.appendChild(root);
  var img = root.querySelector(".whale-img");
  var accEl = root.querySelector(".whale-acc");
  var balEl = root.querySelector(".whale-bal");
  var hintEl = root.querySelector(".whale-hint");
  var bubble = root.querySelector(".whale-bubble");
  var muteBtn = root.querySelector('[data-act="mute"]');
  var refreshBtn = root.querySelector('[data-act="refresh"]');

  // ---- 状态 ----
  var state = { balance: null, currency: "CNY", shown: null, soundOn: true, scale: 1,
                accounts: [], accId: "", pick: 0 };
  try {
    var s = JSON.parse(localStorage.getItem("whalePos") || "{}");
    if (s.scale) { state.scale = s.scale; root.style.setProperty("--whale-scale", s.scale); }
    if (s.soundOn === false) { state.soundOn = false; muteBtn.textContent = "🔇"; }
    if (typeof s.left === "number" && typeof s.top === "number") {
      root.style.left = s.left + "px"; root.style.top = s.top + "px";
      root.style.right = "auto"; root.style.bottom = "auto";
    }
    if (s.accId) state.accId = s.accId;
  } catch (e) {}

  // ---- 音效 ----
  var pressAudio = new Audio(A + "Ya1.mp3");
  var releaseAudio = new Audio(A + "Ya2.mp3");
  pressAudio.volume = releaseAudio.volume = 0.9;
  function playPress() { if (state.soundOn) { try { pressAudio.currentTime = 0; pressAudio.play().catch(function(){}); } catch (e) {} } }
  function playRelease() { if (state.soundOn) { try { releaseAudio.currentTime = 0; releaseAudio.play().catch(function(){}); } catch (e) {} } }

  // ---- 数字滚动动画 ----
  var animId = null;
  function fmt(v, cur) {
    var n = Number(v);
    var f = isFinite(n) ? n.toFixed(2) : "--";
    return cur === "CNY" ? "¥ " + f : f + " " + (cur || "");
  }
  function rollTo(to, cur) {
    if (animId) cancelAnimationFrame(animId);
    var from = (state.shown == null || !isFinite(state.shown)) ? to : state.shown;
    if (from === to) { balEl.textContent = fmt(to, cur); state.shown = to; return; }
    var t0 = null;
    function step(ts) {
      if (t0 === null) t0 = ts;
      var t = Math.min(1, (ts - t0) / 600);
      var e = 1 - Math.pow(1 - t, 3);
      balEl.textContent = fmt(from + (to - from) * e, cur);
      if (t < 1) animId = requestAnimationFrame(step);
      else { animId = null; state.shown = to; balEl.textContent = fmt(to, cur); }
    }
    animId = requestAnimationFrame(step);
  }

  // ---- 气泡 ----
  var bubbleTimer = null;
  function showBubble(html, ms) {
    bubble.innerHTML = html;
    bubble.classList.add("show");
    if (bubbleTimer) clearTimeout(bubbleTimer);
    if (ms > 0) bubbleTimer = setTimeout(function () { bubble.classList.remove("show"); }, ms);
  }

  // ---- 账户名 ----
  function setAccLabel(d) {
    var a = (d && d.account) || state.accounts[state.pick] || null;
    if (!a) { accEl.textContent = ""; return; }
    var n = state.accounts.length;
    accEl.textContent = (n > 1 ? "「" + a.name + "」 " + (state.pick + 1) + "/" + n : a.name);
    accEl.title = n > 1 ? "点鲸鱼切换账户（共 " + n + " 个）" : a.name;
  }

  // ---- 拉余额 ----
  function qs() { return state.accId ? ("?account=" + encodeURIComponent(state.accId)) : ""; }
  function refresh() {
    fetch(BAL_URL + qs(), { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d && d.accounts) {
          state.accounts = d.accounts;
          if (state.accId) {
            var i = state.accounts.findIndex(function (x) { return x.id === state.accId; });
            state.pick = i >= 0 ? i : 0;
            if (i < 0) state.accId = (state.accounts[0] || {}).id || "";
          }
        }
        if (!d || d.ok === false) {
          balEl.textContent = "⚠";
          hintEl.className = "whale-hint whale-err";
          hintEl.textContent = (d && d.error ? d.error : "获取失败");
          setAccLabel(d);
          return;
        }
        state.balance = d.balance; state.currency = d.currency || "CNY";
        if (d.account) state.accId = d.account.id;
        rollTo(d.balance, state.currency);
        var peak = d.isPeak ? "高峰价" : "谷价";
        var tu = (d.todayUsage != null) ? fmt(d.todayUsage, state.currency) : "--";
        var ex = "";
        if (d.extra && d.extra["总额度"] != null) {
          ex = " · 额度 " + fmt(d.extra["总额度"], state.currency);
        }
        hintEl.className = "whale-hint";
        hintEl.textContent = "今日已用 " + tu + " · " + peak + ex;
        setAccLabel(d);
      })
      .catch(function () {
        balEl.textContent = "⚠";
        hintEl.className = "whale-hint whale-err";
        hintEl.textContent = "获取失败";
      });
  }

  // ---- 切换账户 ----
  function nextAccount(showLine) {
    if (state.accounts.length > 1) {
      state.pick = (state.pick + 1) % state.accounts.length;
      state.accId = state.accounts[state.pick].id;
      save();
      state.shown = null;           // 换账户不滚动，直接显示新数字
      refresh();
      var a = state.accounts[state.pick];
      showBubble("换到「" + a.name + "」<br><span style='opacity:.7'>" + (a.keyMask || "") + "</span>", 3000);
    } else {
      refresh();
      showBubble(pick(LINES), 5000);
    }
    if (showLine && state.accounts.length <= 1) showBubble(pick(LINES), 5000);
  }

  // ---- 交互 ----
  img.addEventListener("click", function () { nextAccount(false); });
  refreshBtn.addEventListener("click", function (e) { e.stopPropagation(); refresh(); });
  muteBtn.addEventListener("click", function (e) {
    e.stopPropagation();
    state.soundOn = !state.soundOn;
    muteBtn.textContent = state.soundOn ? "🔊" : "🔇";
    save();
  });

  // 按压音效
  root.addEventListener("pointerdown", function () { root.classList.add("press"); playPress(); });
  root.addEventListener("pointerup", function () { root.classList.remove("press"); playRelease(); });
  root.addEventListener("pointerleave", function () { root.classList.remove("press"); });

  // 拖拽
  var dragging = false, sx = 0, sy = 0, ox = 0, oy = 0;
  root.addEventListener("pointerdown", function (e) {
    if (e.target.closest(".whale-btns")) return;
    dragging = true; root.classList.add("dragging");
    var r = root.getBoundingClientRect();
    sx = e.clientX; sy = e.clientY; ox = r.left; oy = r.top;
    root.style.left = ox + "px"; root.style.top = oy + "px";
    root.style.right = "auto"; root.style.bottom = "auto";
    root.setPointerCapture(e.pointerId);
  });
  root.addEventListener("pointermove", function (e) {
    if (!dragging) return;
    root.style.left = (ox + e.clientX - sx) + "px";
    root.style.top = (oy + e.clientY - sy) + "px";
  });
  root.addEventListener("pointerup", function () {
    if (!dragging) return;
    dragging = false; root.classList.remove("dragging");
    snap(); save();
  });
  function snap() {
    var r = root.getBoundingClientRect();
    var vw = window.innerWidth, vh = window.innerHeight;
    var cx = r.left + r.width / 2;
    root.style.left = (cx < vw / 2 ? 16 : vw - r.width - 16) + "px";
    root.style.top = (vh - r.height - 16) + "px";
  }

  // 滚轮缩放
  root.addEventListener("wheel", function (e) {
    e.preventDefault();
    var n = Math.max(0.6, Math.min(2.5, state.scale - Math.sign(e.deltaY) * 0.15));
    state.scale = Math.round(n * 10) / 10;
    root.style.setProperty("--whale-scale", state.scale);
    save();
  }, { passive: false });

  function save() {
    try {
      var r = root.getBoundingClientRect();
      localStorage.setItem("whalePos", JSON.stringify({
        left: r.left, top: r.top, scale: state.scale,
        soundOn: state.soundOn, accId: state.accId
      }));
    } catch (e) {}
  }
  function pick(arr) { return arr[Math.floor(Math.random() * arr.length)]; }

  /* ================= 设置面板：账户管理 ================= */
  function el(tag, style, text) {
    var d = document.createElement(tag);
    if (style) d.setAttribute("style", style);
    if (text != null) d.textContent = text;
    return d;
  }
  function inp(style, ph) {
    var i = document.createElement("input");
    i.className = "aginput";
    i.setAttribute("style", style || "");
    if (ph) i.placeholder = ph;
    return i;
  }

  function buildSettings(box) {
    var wrap = el("div", "display:flex;flex-direction:column;gap:8px");
    box.appendChild(wrap);

    var listBox = el("div", "display:flex;flex-direction:column;gap:6px");
    var formBox = el("div", "display:none;flex-direction:column;gap:6px;padding:9px;"
                          + "border:1px solid var(--line);border-radius:8px");
    var noteBox = el("div", "font-size:12px;opacity:.65");

    var platforms = [];   // 由后端 accounts 接口带回

    function renderList(accounts) {
      state.accounts = accounts || state.accounts || [];
      listBox.innerHTML = "";
      if (!state.accounts.length) {
        listBox.appendChild(el("div", "font-size:12px;opacity:.6",
          "还没有余额账户。点下面「＋ 添加账户」加一个（DeepSeek 开箱可用）。"));
      }
      state.accounts.forEach(function (a) {
        var row = el("div", "display:flex;align-items:center;gap:8px;font-size:12.5px;"
                          + "padding:6px 8px;border:1px solid var(--line);border-radius:8px");
        var cur = (a.id === state.accId);
        row.appendChild(el("span", "flex:0 0 auto", cur ? "🔵" : "⚪"));
        row.appendChild(el("span", "flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap",
          a.name + (a.inherited ? "（读工作台默认 Key）" : "")));
        row.appendChild(el("span", "flex:0 0 auto;opacity:.6;font-size:11.5px",
          (a.platform || "") + " " + (a.keyMask || "")));
        if (!cur) {
          var bUse = document.createElement("button");
          bUse.className = "btn"; bUse.textContent = "显示";
          bUse.onclick = function () {
            state.accId = a.id;
            state.pick = state.accounts.findIndex(function (x) { return x.id === a.id; });
            state.shown = null; save(); refresh(); renderList(state.accounts);
          };
          row.appendChild(bUse);
        }
        if (!a.inherited) {
          var bEdit = document.createElement("button");
          bEdit.className = "btn"; bEdit.textContent = "改";
          bEdit.onclick = function () { openForm(a); };
          row.appendChild(bEdit);

          var bDel = document.createElement("button");
          bDel.className = "btn"; bDel.textContent = "删";
          bDel.onclick = function () {
            if (!confirm("删除账户「" + a.name + "」？")) return;
            WB.post(ACC_URL, { action: "delete", id: a.id }).then(function (r) {
              if (r && r.error) { alert(r.error); return; }
              if (state.accId === a.id) { state.accId = ""; state.shown = null; }
              renderList(r && r.accounts);
              refresh();
            });
          };
          row.appendChild(bDel);
        }
        listBox.appendChild(row);
      });
    }

    function openForm(a) {
      formBox.style.display = "flex";
      formBox.innerHTML = "";
      formBox.appendChild(el("div", "font-size:12.5px;font-weight:600",
        a ? ("编辑账户：" + a.name) : "添加余额账户"));

      var nameI = inp("width:100%", "给这个账户起个名，比如 DeepSeek 主号");
      nameI.value = a ? a.name : "";

      var platSel = document.createElement("select");
      platSel.className = "aginput";
      platSel.setAttribute("style", "width:100%");
      (platforms || []).forEach(function (p) {
        var o = document.createElement("option");
        o.value = p.id; o.textContent = p.name + " —— " + (p.hint || "");
        platSel.appendChild(o);
      });
      var custOpt = document.createElement("option");
      custOpt.value = "custom"; custOpt.textContent = "自定义 / 其它平台";
      platSel.appendChild(custOpt);
      platSel.value = (a && a.platform) || "deepseek";

      var urlI = inp("width:100%", "余额接口地址");
      urlI.value = a ? (a.url || "") : "https://api.deepseek.com/user/balance";

      var keyI = inp("width:100%", a ? "留空 = 不修改已保存的 Key" : "粘贴 API Key");
      keyI.type = "password"; keyI.autocomplete = "off";

      var platHint = el("div", "font-size:11.5px;opacity:.6");

      function syncUrl() {
        var pid = platSel.value;
        var hit = (platforms || []).find(function (p) { return p.id === pid; });
        platHint.textContent = hit ? (hit.hint || "") : "";
        if (pid !== "custom" && hit && hit.url) urlI.value = hit.url;
      }
      platSel.onchange = syncUrl;
      syncUrl();

      var row1 = el("div", "display:flex;flex-direction:column;gap:4px");
      row1.appendChild(el("label", "font-size:12px;opacity:.75", "名称"));
      row1.appendChild(nameI);
      row1.appendChild(el("label", "font-size:12px;opacity:.75;margin-top:4px", "平台"));
      row1.appendChild(platSel);
      row1.appendChild(platHint);
      row1.appendChild(el("label", "font-size:12px;opacity:.75;margin-top:4px", "余额接口地址"));
      row1.appendChild(urlI);
      row1.appendChild(el("label", "font-size:12px;opacity:.75;margin-top:4px", "API Key"));
      row1.appendChild(keyI);
      formBox.appendChild(row1);

      var btns = el("div", "display:flex;gap:7px;margin-top:6px");
      var bSave = document.createElement("button");
      bSave.className = "btn"; bSave.textContent = "保存";
      bSave.onclick = function () {
        var body = { action: "save", account: {
          id: a ? a.id : "",
          name: nameI.value.trim(),
          platform: platSel.value,
          url: urlI.value.trim(),
          key: keyI.value.trim()
        }};
        WB.post(ACC_URL, body).then(function (r) {
          if (r && r.error) { alert(r.error); return; }
          formBox.style.display = "none";
          renderList(r && r.accounts);
          if (r && r.id && !state.accId) state.accId = r.id;   // 第一个账户自动选中
          state.shown = null;
          refresh();
        });
      };
      var bCancel = document.createElement("button");
      bCancel.className = "btn"; bCancel.textContent = "取消";
      bCancel.onclick = function () { formBox.style.display = "none"; };
      btns.appendChild(bSave); btns.appendChild(bCancel);
      formBox.appendChild(btns);
    }

    var bAdd = document.createElement("button");
    bAdd.className = "btn"; bAdd.textContent = "＋ 添加账户";
    bAdd.onclick = function () {
      var used = {};
      state.accounts.forEach(function (a) { used[a.platform] = 1; });
      openForm(null);
    };

    noteBox.textContent = "每个账户 = 名称 + 余额接口 + Key。点挂件上的小鲸鱼可切换显示哪个账户。";

    wrap.appendChild(listBox);
    wrap.appendChild(bAdd);
    wrap.appendChild(formBox);
    wrap.appendChild(noteBox);

    WB.get(ACC_URL).then(function (r) {
      if (!r || !r.ok) return;
      platforms = r.platforms || [];
      if (!state.accId && r.accounts && r.accounts.length) state.accId = r.accounts[0].id;
      renderList(r.accounts);
    }).catch(function () {});
  }

  if (window.WB && WB.addSettingSection) {
    try { WB.addSettingSection("小鲸鱼余额（多平台多 Key）", buildSettings); } catch (e) {}
  }

  // 启动
  refresh();
  setInterval(refresh, REFRESH_MS);
})();
