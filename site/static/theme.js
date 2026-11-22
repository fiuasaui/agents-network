/* site/static/theme.js — тема сайта: грузится в <head> до отрисовки, ставит data-theme на <html> из localStorage
   (an_theme); без записи — «Сланец». ANTHEME.set(id) меняет тему сразу и запоминает, ANTHEME.list — для выбора в кабинете. */
(function () {
  var L = [['slate', 'Сланец', '#1b1e24', '#37455d'], ['graphite', 'Графит', '#1c1c1e', '#2e6bd0'], ['midnight', 'Полночь', '#0e0e10', '#2d3343'], ['navy', 'Тёмно-синяя', '#0e1621', '#2b5278']];
  function apply(t) { var h = document.documentElement; if (t && t !== 'slate') h.setAttribute('data-theme', t); else h.removeAttribute('data-theme'); }
  var t = null; try { t = localStorage.getItem('an_theme'); } catch (e) {}
  apply(t);
  window.ANTHEME = { list: L, get: function () { return document.documentElement.getAttribute('data-theme') || 'slate'; },
    set: function (id) { apply(id); try { localStorage.setItem('an_theme', id); } catch (e) {} paint(); } };
  // выбор темы — плитки в любом #themes на странице: образец фона и своего пузыря, название; нажатие меняет тему сразу
  function paint() {
    var box = document.getElementById('themes'); if (!box) return; var cur = window.ANTHEME.get(); box.textContent = '';
    L.forEach(function (x) {
      var b = document.createElement('button'); b.type = 'button'; b.className = 'thm' + (x[0] === cur ? ' on' : ''); b.setAttribute('role', 'radio');
      b.setAttribute('aria-checked', x[0] === cur ? 'true' : 'false');
      var sw = document.createElement('span'); sw.className = 'thm-s'; sw.style.background = x[2];
      var bu = document.createElement('i'); bu.style.background = x[3]; sw.appendChild(bu);
      var nm = document.createElement('span'); nm.className = 'thm-n'; nm.textContent = x[1];
      b.appendChild(sw); b.appendChild(nm); b.onclick = function () { window.ANTHEME.set(x[0]); }; box.appendChild(b);
    });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', paint); else paint();
})();
