'use strict';
const audio = document.querySelector('audio');
if (audio) audio.addEventListener('error', () => { document.querySelector('.media-error').hidden = false; });
const image = document.querySelector('.spectrogram');
if (image) {
  const state = document.querySelector('.spectrogram-state');
  const retry = document.querySelector('.retry-image');
  const loaded = () => { state.textContent = 'Spectrogram ready.'; retry.hidden = true; image.hidden = false; };
  const failed = () => { state.textContent = 'Spectrogram unavailable. Audio and metadata are still accessible.'; retry.hidden = false; image.hidden = true; };
  image.addEventListener('load', loaded);
  image.addEventListener('error', failed);
  if (image.complete) image.naturalWidth ? loaded() : failed();
  retry.addEventListener('click', () => { state.textContent = 'Loading spectrogram…'; image.src = image.src.split('?')[0] + '?retry=' + Date.now(); });
}
// Only overview activity and system status refresh. Review forms and players never get replaced.
const live = document.querySelector('[data-live]');
if (live) {
  const interval = Number(document.body.dataset.refresh) * 1000;
  const state = document.getElementById('refresh-status');
  const refresh = async () => {
    if (!document.hidden && !live.contains(document.activeElement)) {
      try {
        const response = await fetch(location.href, {signal: AbortSignal.timeout(8000)});
        if (!response.ok) throw new Error('unavailable');
        const doc = new DOMParser().parseFromString(await response.text(), 'text/html');
        const replacement = doc.querySelector('[data-live]');
        if (!replacement) throw new Error('unavailable');
        live.replaceChildren(...replacement.childNodes);
        state.textContent = 'Updated ' + new Date().toLocaleTimeString() + '. Refreshes every ' + interval / 1000 + ' seconds.';
      } catch (_) { state.textContent = 'Refresh unavailable; showing previous results. Retrying shortly.'; }
    }
    setTimeout(refresh, interval);
  };
  setTimeout(refresh, interval);
}
