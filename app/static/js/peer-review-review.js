// Image + overlay confirmation for anonymous peer-review scans.
//
// Mirrors the normal scan-review overlay but works against the peer-review coordinate
// map (rating_bubbles keyed by target_student_id + competency_id + score) and the
// per-submission rating map. All geometry comes from the saved coordinate map so the
// overlay lines up with the exact printed sheet.

(function () {
  function postAction(url, params) {
    var form = document.createElement('form');
    form.method = 'post';
    form.action = url;
    Object.keys(params || {}).forEach(function (key) {
      var input = document.createElement('input');
      input.type = 'hidden';
      input.name = key;
      input.value = params[key];
      form.appendChild(input);
    });
    document.body.appendChild(form);
    form.submit();
  }

  function readJson(id) {
    var el = document.getElementById(id);
    if (!el) return null;
    try {
      return JSON.parse(el.textContent);
    } catch (err) {
      return null;
    }
  }

  function addControl(overlay, pw, ph, xMm, yMm, label, cls, sizePx, onClick, title) {
    var ctl = document.createElement('div');
    ctl.className = 'bubble-ctl ' + cls;
    ctl.textContent = label;
    ctl.title = title || '';
    ctl.style.left = (xMm / pw * 100) + '%';
    ctl.style.top = (yMm / ph * 100) + '%';
    ctl.style.width = sizePx + 'px';
    ctl.style.height = sizePx + 'px';
    ctl.style.fontSize = Math.round(sizePx * 0.6) + 'px';
    ctl.addEventListener('click', onClick);
    overlay.appendChild(ctl);
  }

  function render() {
    var wrapper = document.getElementById('pr-review-wrapper');
    var img = document.getElementById('pr-review-image');
    var overlay = document.getElementById('pr-review-overlay');
    if (!wrapper || !img || !overlay) return;

    var cmap = readJson('pr-review-cmap');
    if (!cmap) return;
    var rmap = readJson('pr-review-rmap') || {};

    var base = wrapper.dataset.base || '';
    var blankLabel = wrapper.dataset.blankLabel || 'Blank';
    var invalidLabel = wrapper.dataset.invalidLabel || 'Invalid';

    overlay.innerHTML = '';
    var pw = cmap.page_width_mm || 210;
    var ph = cmap.page_height_mm || 297;
    var bubbles = cmap.rating_bubbles || [];

    var imgRect = img.getBoundingClientRect();
    var wrapRect = wrapper.getBoundingClientRect();
    overlay.style.left = (imgRect.left - wrapRect.left) + 'px';
    overlay.style.top = (imgRect.top - wrapRect.top) + 'px';
    overlay.style.width = imgRect.width + 'px';
    overlay.style.height = imgRect.height + 'px';

    // Group bubbles per (target, competency) cell so blank/invalid controls can be
    // placed just past the last bubble of the active cell.
    var cells = {};
    bubbles.forEach(function (b) {
      var key = b.target_student_id + '_' + b.competency_id;
      (cells[key] = cells[key] || []).push(b);
    });

    bubbles.forEach(function (b) {
      var key = b.target_student_id + '_' + b.competency_id;
      var info = rmap[key] || null;
      var dot = document.createElement('div');
      dot.className = 'bubble-dot';
      dot.style.left = (b.x_mm / pw * 100) + '%';
      dot.style.top = (b.y_mm / ph * 100) + '%';
      var rPx = Math.max(6, b.radius_mm / pw * imgRect.width);
      dot.style.width = (rPx * 2) + 'px';
      dot.style.height = (rPx * 2) + 'px';
      var scoreStr = String(b.score);

      if (info) {
        var chosen = info.resolved ? info.final : info.detected;
        var isChosen = chosen != null && String(chosen) === scoreStr;
        if (info.resolved) {
          dot.classList.add(isChosen ? 'bubble-correct' : 'bubble-empty');
        } else {
          dot.classList.add(isChosen ? 'bubble-wrong' : 'bubble-empty');
        }
        if (info.is_current) {
          dot.classList.add('bubble-is-current');
          dot.classList.add('bubble-choice-clickable');
          dot.title = scoreStr;
          (function (ratingId, score) {
            dot.addEventListener('click', function () {
              postAction(base + '/review/' + ratingId, { score: score, status: 'reviewed' });
            });
          })(info.rating_id, scoreStr);
        } else {
          dot.classList.add('bubble-clickable');
          (function (ratingId) {
            dot.addEventListener('click', function () {
              window.location.href = base + '/review?rating_id=' + ratingId;
            });
          })(info.rating_id);
        }
      } else {
        dot.classList.add('bubble-ghost');
      }
      overlay.appendChild(dot);
    });

    Object.keys(cells).forEach(function (key) {
      var info = rmap[key];
      if (!info || !info.is_current) return;
      var arr = cells[key];
      var last = arr[arr.length - 1];
      var ctlSize = Math.max(14, last.radius_mm / pw * imgRect.width * 2);
      addControl(
        overlay, pw, ph, last.x_mm + last.radius_mm + 4.5, last.y_mm, '×', 'bubble-ctl-blank',
        ctlSize, function () { postAction(base + '/review/' + info.rating_id, { status: 'blank' }); }, blankLabel
      );
      addControl(
        overlay, pw, ph, last.x_mm + last.radius_mm + 10, last.y_mm, '!', 'bubble-ctl-invalid',
        ctlSize, function () { postAction(base + '/review/' + info.rating_id, { status: 'invalid' }); }, invalidLabel
      );
    });
  }

  document.addEventListener('DOMContentLoaded', function () {
    var img = document.getElementById('pr-review-image');
    if (!img) return;
    if (img.complete && img.naturalWidth) {
      render();
    } else {
      img.addEventListener('load', render);
    }
    window.addEventListener('resize', render);
  });
})();
