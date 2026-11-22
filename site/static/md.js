/* md.js — письма агентов в Markdown чат рисует структурно (свой маленький рендер, без зависимостей). */
/* md.js — маленький безопасный Markdown → HTML для ответов ассистента.
   Весь текст сначала экранируется, разметка собирается только из своих тегов; ссылки — только http(s).
   Поддержано: ``` блоки кода ```, заголовки, списки (в т.ч. вложенные отступом), цитаты, таблицы, hr, абзацы;
   внутри строки — `код`, **жирный**, *курсив*, ~~зачёркнутый~~, [текст](url), голые https-ссылки. */
(function () {
  const esc = s => s.replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  function inline(s) {
    const codes = [];
    s = s.replace(/`([^`\n]+)`/g, (_, c) => { codes.push(c); return "\u0000" + (codes.length - 1) + "\u0000"; });
    s = esc(s);
    s = s.replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g, (_, t, u) => `<a href="${u}" target="_blank" rel="noopener nofollow">${t}</a>`);
    s = s.replace(/\[([^\]\n]+)\]\((?!https?:)[^)\s]+\)/g, "<b>$1</b>");
    s = s.replace(/(^|[\s(])(https?:\/\/[^\s<)]+[^\s<).,;:!?])/g, (_, p, u) => `${p}<a href="${u}" target="_blank" rel="noopener nofollow">${u}</a>`);
    s = s.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>").replace(/__([^_\n]+)__/g, "<strong>$1</strong>");
    s = s.replace(/(^|[^*\w])\*([^*\n]+)\*(?!\*)/g, "$1<em>$2</em>").replace(/(^|[^_\w])_([^_\n]+)_(?!\w)/g, "$1<em>$2</em>");
    s = s.replace(/~~([^~\n]+)~~/g, "<del>$1</del>");
    return s.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${esc(codes[+i])}</code>`);
  }

  function table(rows) {
    const cells = r => r.replace(/^\s*\|/, "").replace(/\|\s*$/, "").split("|").map(c => c.trim());
    const head = cells(rows[0]);
    let h = "<table><thead><tr>" + head.map(c => `<th>${inline(c)}</th>`).join("") + "</tr></thead><tbody>";
    for (const r of rows.slice(2)) h += "<tr>" + cells(r).map(c => `<td>${inline(c)}</td>`).join("") + "</tr>";
    return h + "</tbody></table>";
  }

  function list(lines) {
    // lines: [{indent, ordered, text}] → вложенные списки по отступу
    let html = "", stack = [];
    for (const it of lines) {
      while (stack.length && it.indent < stack[stack.length - 1].indent) { html += `</li></${stack.pop().tag}>`; }
      let top = stack[stack.length - 1];
      if (top && it.indent === top.indent && top.tag !== (it.ordered ? "ol" : "ul")) {    // «1.» после «-» — новый список, а не пункт старого
        html += `</li></${stack.pop().tag}>`; top = stack[stack.length - 1];
      }
      if (!top || it.indent > top.indent) {
        const tag = it.ordered ? "ol" : "ul";
        stack.push({ indent: it.indent, tag });
        html += `<${tag}${it.ordered && it.start > 1 ? ` start="${it.start}"` : ""}><li>`;
      } else {
        html += "</li><li>";
      }
      html += inline(it.text);
    }
    while (stack.length) html += `</li></${stack.pop().tag}>`;
    return html;
  }

  function render(src) {
    const lines = String(src || "").replace(/\r\n?/g, "\n").split("\n");
    let out = "", i = 0, para = [];
    const flush = () => { if (para.length) { out += "<p>" + para.map(inline).join("<br>") + "</p>"; para = []; } };
    while (i < lines.length) {
      const line = lines[i];
      const fence = line.match(/^\s*(```|~~~)\s*([\w+#.-]*)\s*$/);
      if (fence) {
        flush();
        const lang = fence[2] || "", body = [];
        i++;
        while (i < lines.length && !lines[i].match(new RegExp("^\\s*" + fence[1] + "\\s*$"))) body.push(lines[i++]);
        i++;
        out += `<div class="code"><div class="code-head"><span>${esc(lang || "code")}</span><button type="button" data-copy-code>` +
          `<svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a1 1 0 0 1 1-1h9"/></svg><span>Копировать</span></button></div>` +
          `<pre><code>${esc(body.join("\n"))}</code></pre></div>`;
        continue;
      }
      if (/^\s*$/.test(line)) { flush(); i++; continue; }
      const hd = line.match(/^(#{1,6})\s+(.*)$/);
      if (hd) { flush(); const n = Math.min(hd[1].length, 4); out += `<h${n}>${inline(hd[2])}</h${n}>`; i++; continue; }
      if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) { flush(); out += "<hr>"; i++; continue; }
      if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1])) {
        flush(); const rows = [];
        while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) rows.push(lines[i++]);
        out += table(rows); continue;
      }
      if (/^\s*>/.test(line)) {
        flush(); const q = [];
        while (i < lines.length && /^\s*>/.test(lines[i])) q.push(lines[i++].replace(/^\s*>\s?/, ""));
        out += "<blockquote>" + render(q.join("\n")) + "</blockquote>"; continue;
      }
      const li = line.match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
      if (li) {
        flush(); const items = [];
        while (i < lines.length) {
          const m = lines[i].match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
          if (m) { items.push({ indent: m[1].length, ordered: /\d/.test(m[2]), start: parseInt(m[2]) || 1, text: m[3] }); i++; }
          else if (/^\s{2,}\S/.test(lines[i]) && items.length) { items[items.length - 1].text += " " + lines[i].trim(); i++; }
          else break;
        }
        out += list(items); continue;
      }
      para.push(line); i++;
    }
    flush();
    return out;
  }
  // один класс — один вид разметки на всех экранах (md.css)
  window.renderMarkdown = src => { const out = render(src); return out ? '<div class="mdx">' + out + "</div>" : out; };
  // «Копировать» у блока кода — один обработчик на страницу
  if (!window.__mdCopy) {
    window.__mdCopy = true;
    document.addEventListener("click", e => {
      const b = e.target.closest && e.target.closest("[data-copy-code]");
      if (!b) return;
      e.preventDefault(); e.stopPropagation();
      const pre = b.closest(".code") && b.closest(".code").querySelector("pre"), lbl = b.querySelector("span");
      const done = ok => { if (!lbl) return; b.classList.toggle("ok", ok); lbl.textContent = ok ? "Скопировано" : "Не вышло"; setTimeout(() => { b.classList.remove("ok"); lbl.textContent = "Копировать"; }, 1500); };
      const txt = pre ? pre.innerText : "";
      if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(txt).then(() => done(true), () => done(false));
      else { const t = document.createElement("textarea"); t.value = txt; t.style.position = "fixed"; t.style.opacity = "0"; b.parentNode.appendChild(t); t.select();      // рядом с кнопкой: в окне карточки body лежит под ним
             let ok = false; try { ok = document.execCommand("copy"); } catch (_) {} t.remove(); done(ok); }
    }, true);
  }
})();
