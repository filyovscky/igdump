/* All report interactions use local data. No network requests. */
(() => {
  'use strict';
  const make = (tag, text, className) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  };
  const analytics = document.getElementById('analytics-data');
  if (analytics) {
    const original = JSON.parse(analytics.textContent);
    const select = document.getElementById('timezone');
    const charts = document.getElementById('analytics-charts');
    const local = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
    const zones = new Set(['UTC', local, 'Europe/Moscow', 'Europe/London', 'America/New_York', 'Asia/Tokyo']);
    if (Intl.supportedValuesOf) Intl.supportedValuesOf('timeZone').forEach(zone => zones.add(zone));
    select.replaceChildren();
    [...zones].sort().forEach(zone => {
      const option = make('option', zone + (zone === local ? ' (browser local)' : ''));
      option.value = zone;
      select.append(option);
    });
    select.value = local;
    let current;
    const bars = (labels, values, title) => {
      charts.append(make('h2', title));
      const group = make('div', undefined, 'bars');
      const maximum = Math.max(1, ...values);
      labels.forEach((label, i) => {
        const row = make('div', undefined, 'bar-row');
        const track = make('div', undefined, 'bar-track');
        const bar = make('div', undefined, 'bar');
        bar.style.width = `${values[i] / maximum * 100}%`;
        track.append(bar);
        row.append(make('span', label), track, make('span', String(values[i])));
        group.append(row);
      });
      charts.append(group);
    };
    const render = () => {
      const zone = select.value;
      const formatter = new Intl.DateTimeFormat('en-GB', {timeZone: zone,
        year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', hourCycle: 'h23'});
      const daily = {}, monthly = {}, heatmap = Array.from({length: 7}, () => Array(24).fill(0));
      original.timestamps.forEach(stamp => {
        const parts = Object.fromEntries(formatter.formatToParts(new Date(stamp * 1000)).map(p => [p.type, p.value]));
        const day = `${parts.year}-${parts.month}-${parts.day}`;
        const month = day.slice(0, 7);
        const weekday = (new Date(Date.UTC(+parts.year, +parts.month - 1, +parts.day)).getUTCDay() + 6) % 7;
        daily[day] = (daily[day] || 0) + 1;
        monthly[month] = (monthly[month] || 0) + 1;
        heatmap[weekday][+parts.hour]++;
      });
      const dates = Object.keys(daily).sort(), first = dates[0], last = dates.at(-1);
      const span = first ? Math.round((Date.parse(last) - Date.parse(first)) / 86400000) + 1 : 0;
      if (first) {
        let [year, month] = first.slice(0, 7).split('-').map(Number);
        while (`${year}-${String(month).padStart(2, '0')}` <= last.slice(0, 7)) {
          const key = `${year}-${String(month).padStart(2, '0')}`;
          monthly[key] ??= 0;
          if (++month > 12) { month = 1; year++; }
        }
      }
      const hours = Array.from({length: 24}, (_, h) => heatmap.reduce((n, row) => n + row[h], 0));
      const weekdays = heatmap.map(row => row.reduce((a, b) => a + b, 0));
      current = {...original, timezone: zone, first_day: first || null, last_day: last || null,
        observed_days: span, active_days: dates.length,
        posts_per_day: span ? Math.round(original.dated_posts / span * 1000) / 1000 : null,
        posts_per_week: span ? Math.round(original.dated_posts * 7 / span * 1000) / 1000 : null,
        daily: Object.fromEntries(dates.map(d => [d, daily[d]])),
        monthly: Object.fromEntries(Object.keys(monthly).sort().map(m => [m, monthly[m]])),
        heatmap, hours, weekdays};
      const offset = new Intl.DateTimeFormat('en', {timeZone: zone, timeZoneName: 'shortOffset'});
      const offsets = [...new Set(original.timestamps.map(t => offset.formatToParts(new Date(t * 1000)).find(p => p.type === 'timeZoneName').value))];
      document.getElementById('timezone-note').textContent = `All hours, weekdays and dates below use ${zone} (${offsets.join(', ')} over these publication dates). Daylight saving is applied per date. These are publication times, not audience activity. The selection of posts stays the same.`;
      charts.replaceChildren();
      charts.append(make('p', `${original.dated_posts} dated posts · ${dates.length} active days / ${span} calendar days · ${current.posts_per_day} posts/day · ${current.posts_per_week} posts/week. Observed span: ${first} through ${last}. Zero-post days are included in the rate.`));
      bars(Object.keys(current.monthly), Object.values(current.monthly), 'Posts by month');
      const days = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];
      bars(days, weekdays, 'Posts by weekday');
      bars(hours.map((_, h) => `${String(h).padStart(2, '0')}:00`), hours, `Posts by hour — ${zone}`);
      charts.append(make('h2', `Weekday × hour — ${zone}`));
      const scroll = make('div', undefined, 'table-scroll'), table = make('table', undefined, 'heatmap');
      const head = make('thead'), heading = make('tr');
      heading.append(make('th', 'Day / hour'));
      hours.forEach((_, h) => heading.append(make('th', String(h).padStart(2, '0'))));
      head.append(heading);
      const body = make('tbody'), maximum = Math.max(1, ...heatmap.flat());
      heatmap.forEach((values, day) => {
        const row = make('tr');
        row.append(make('th', days[day].slice(0, 3)));
        values.forEach((count, hour) => {
          const cell = make('td', String(count));
          cell.style.background = `rgba(192,46,101,${0.08 + count / maximum * 0.72})`;
          cell.title = `${days[day]} ${String(hour).padStart(2, '0')}:00 (${zone}): ${count} posts`;
          row.append(cell);
        });
        body.append(row);
      });
      table.append(head, body); scroll.append(table); charts.append(scroll);
    };
    select.addEventListener('change', render);
    render();
    const download = (body, type, name) => {
      const url = URL.createObjectURL(new Blob([body], {type}));
      const link = make('a'); link.href = url; link.download = name; link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    };
    document.getElementById('analytics-json').addEventListener('click', () => download(JSON.stringify(current, null, 2), 'application/json', 'analytics-selected-zone.json'));
    document.getElementById('analytics-csv').addEventListener('click', () => {
      const rows = ['date,posts,timezone'];
      for (let i = 0; i < current.observed_days; i++) {
        const day = new Date(Date.parse(current.first_day) + i * 86400000).toISOString().slice(0, 10);
        rows.push(`${day},${current.daily[day] || 0},${select.value}`);
      }
      download('\uFEFF' + rows.join('\r\n') + '\r\n', 'text/csv;charset=utf-8', 'analytics-selected-zone.csv');
    });
  }
  const map = document.getElementById('location-map');
  if (map) {
    const points = JSON.parse(document.getElementById('map-data').textContent);
    const records = JSON.parse(document.getElementById('map-records').textContent);
    const grouped = new Map();
    records.forEach(row => {
      const key = `${row.latitude},${row.longitude}`;
      if (!grouped.has(key)) grouped.set(key, []);
      grouped.get(key).push(row);
    });
    const groups = [...grouped.values()];
    const dateLabel = group => {
      const dates = [...new Set(group.filter(row => row.published_at).map(row => row.published_at.slice(0, 10)))].sort();
      const label = dates.length > 1 ? `${dates[0]} → ${dates.at(-1)}` : dates[0] || 'Date unknown';
      return label + (group.length > 1 ? ` (${group.length} posts)` : '');
    };
    const details = group => {
      const popup = make('div', undefined, 'pin-details');
      popup.append(make('p', `${group[0].latitude}, ${group[0].longitude}`));
      group.forEach(row => {
        const entry = make('p');
        entry.append(make('strong', row.name || 'Unnamed place'), make('br'),
          make('span', row.published_at || 'Date unknown'), make('br'));
        const link = make('a', `Source post: ${row.post_shortcode}`);
        link.href = row.source_url; entry.append(link); popup.append(entry);
      });
      return popup;
    };
    const showDetails = pin => {
      if (pin) document.getElementById('offline-pin-details').replaceChildren(details(groups[+pin.dataset.locationGroup]));
    };
    const street = document.getElementById('street-map');
    const toggle = document.getElementById('street-map-toggle');
    const datesToggle = document.getElementById('pin-dates');
    datesToggle.addEventListener('change', () => {
      map.querySelectorAll('.pin-date').forEach(label => { label.style.display = datesToggle.checked ? '' : 'none'; });
      street.classList.toggle('hide-pin-dates', !datesToggle.checked);
    });
    let streetMap;
    toggle.addEventListener('click', () => {
      const opening = street.hidden;
      street.hidden = !opening;
      if (opening) map.setAttribute('hidden', '');
      else map.removeAttribute('hidden');
      document.getElementById('offline-map-controls').hidden = opening;
      toggle.textContent = opening ? 'Show offline map' : 'Load street map (Internet)';
      if (!opening) return;
      if (!streetMap) {
        streetMap = L.map(street, {scrollWheelZoom: false});
        L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
          maxZoom: 19, keepBuffer: 1, updateWhenIdle: true,
          attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
        }).addTo(streetMap);
        groups.forEach(group => {
          const row = group[0];
          L.circleMarker([row.latitude, row.longitude], {radius: 6, color: '#c02e65', fillOpacity: 0.8})
            .addTo(streetMap).bindPopup(details(group))
            .bindTooltip(make('span', dateLabel(group)), {permanent: true, direction: 'right', className: 'pin-date-tooltip'});
        });
        if (records.length) streetMap.fitBounds(records.map(row => [row.latitude, row.longitude]), {padding: [24, 24], maxZoom: 14});
        else streetMap.setView([20, 0], 2);
        const arrangeTooltips = () => {
          const occupied = [];
          street.querySelectorAll('.pin-date-tooltip').forEach(label => {
            label.style.marginTop = '0px';
            const rect = label.getBoundingClientRect();
            let shift = 0;
            while (occupied.some(other => rect.left < other.right && rect.right > other.left && rect.top + shift < other.bottom && rect.bottom + shift > other.top)) shift += rect.height + 3;
            label.style.marginTop = `${shift}px`;
            occupied.push({left: rect.left, right: rect.right, top: rect.top + shift, bottom: rect.bottom + shift});
          });
        };
        streetMap.on('zoomend moveend', arrangeTooltips);
        arrangeTooltips();
      }
      streetMap.invalidateSize();
    });
    const world = [-8, -8, 736, 376];
    let view = world.slice();
    const draw = () => {
      map.setAttribute('viewBox', view.join(' '));
      if (!map.clientWidth || !map.clientHeight) return;
      const radius = Math.max(view[2] / map.clientWidth, view[3] / map.clientHeight) * 5;
      map.querySelectorAll('circle').forEach(circle => circle.setAttribute('r', String(radius)));
      map.querySelectorAll('.location-pin').forEach(pin => {
        const circle = pin.querySelector('circle'), label = pin.querySelector('.pin-date');
        label.setAttribute('x', String(+circle.getAttribute('cx') + radius * 1.5));
        label.setAttribute('y', String(+circle.getAttribute('cy') - radius * 1.5));
        label.style.fontSize = `${radius * 2.4}px`;
        label.style.strokeWidth = `${radius * 0.6}px`;
      });
      const occupied = [];
      map.querySelectorAll('.pin-date').forEach(label => {
        const box = label.getBBox(), gap = radius * 0.6;
        let y = box.y;
        while (occupied.some(other => box.x < other.x + other.width + gap && box.x + box.width + gap > other.x && y < other.y + other.height + gap && y + box.height + gap > other.y)) y += box.height + gap;
        label.setAttribute('y', String(+label.getAttribute('y') + y - box.y));
        occupied.push({x: box.x, y, width: box.width, height: box.height});
      });
    };
    const fit = () => {
      if (!points.length) { view = world.slice(); draw(); return; }
      const xs = points.map(p => p[0]), ys = points.map(p => p[1]);
      const w = Math.max(32, Math.max(...xs) - Math.min(...xs) + 20);
      const h = Math.max(20, Math.max(...ys) - Math.min(...ys) + 20);
      view = [(Math.max(...xs) + Math.min(...xs) - w) / 2, (Math.max(...ys) + Math.min(...ys) - h) / 2, w, h];
      draw();
    };
    const zoom = factor => {
      const width = Math.min(1472, Math.max(2, view[2] * factor)), ratio = width / view[2];
      view = [view[0] + view[2] * (1 - ratio) / 2, view[1] + view[3] * (1 - ratio) / 2, width, view[3] * ratio];
      draw();
    };
    document.querySelectorAll('[data-map]').forEach(button => button.addEventListener('click', () => {
      const action = button.dataset.map;
      if (action === 'fit') fit();
      else if (action === 'world') { view = world.slice(); draw(); }
      else zoom(action === 'in' ? 0.7 : 1 / 0.7);
    }));
    let drag, pressedPin, moved = false;
    map.addEventListener('pointerdown', event => {
      if (event.button !== 0) return;
      pressedPin = event.target.closest('.location-pin');
      drag = {x: event.clientX, y: event.clientY, view: view.slice(), matrix: map.getScreenCTM().inverse()};
      moved = false;
      map.setPointerCapture(event.pointerId);
    });
    map.addEventListener('pointermove', event => {
      if (!drag) return;
      const dx = event.clientX - drag.x, dy = event.clientY - drag.y;
      if (Math.abs(dx) + Math.abs(dy) > 4) moved = true;
      view = [drag.view[0] - dx * drag.matrix.a, drag.view[1] - dy * drag.matrix.d, drag.view[2], drag.view[3]];
      draw();
    });
    map.addEventListener('pointerup', () => { drag = null; });
    map.addEventListener('pointercancel', () => { drag = null; });
    map.addEventListener('click', event => {
      if (moved) { event.preventDefault(); moved = false; return; }
      showDetails(event.target.closest('.location-pin') || pressedPin);
      pressedPin = null;
    });
    map.addEventListener('keydown', event => {
      if (event.key === 'Enter' || event.key === ' ') showDetails(event.target.closest('.location-pin'));
      else if (event.key === '+' || event.key === '=') zoom(0.7);
      else if (event.key === '-') zoom(1 / 0.7);
      else if (event.key === 'Home') fit();
      else return;
      event.preventDefault();
    });
    window.addEventListener('resize', draw);
    fit();
  }
})();
