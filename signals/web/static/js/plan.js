/**
 * 盘前计划 + 周末策略页 — plan.js
 */

let _planLoaded = false;

window.loadPlanPage = function () {
  if (!_planLoaded) {
    document.getElementById('plan-generate-btn').addEventListener('click', _generatePlan);
    document.getElementById('plan-weekly-btn').addEventListener('click', _loadWeekly);
    _planLoaded = true;
  }
};

/* ── 生成盘前计划 ── */
async function _generatePlan() {
  const btn = document.getElementById('plan-generate-btn');
  btn.disabled = true;
  btn.textContent = '生成中...';

  try {
    const res = await fetch(API_BASE + '/api/plan/generate', { method: 'POST' });
    const data = await res.json();
    if (!data.ok) {
      showToast(data.error || '生成失败', 3000);
      return;
    }
    _renderPlans(data.plans);
  } catch (e) {
    showToast('请求失败: ' + e.message, 3000);
  } finally {
    btn.disabled = false;
    btn.textContent = '生成盘前计划';
  }
}

/* ── 加载周末策略 ── */
async function _loadWeekly() {
  const btn = document.getElementById('plan-weekly-btn');
  btn.disabled = true;
  btn.textContent = '加载中...';

  try {
    const data = await apiFetch('/api/plan/weekly');
    _renderWeekly(data);
  } catch (e) {
    showToast('请求失败: ' + e.message, 3000);
  } finally {
    btn.disabled = false;
    btn.textContent = '周末策略';
  }
}

/* ── 渲染盘前计划 ── */
function _renderPlans(plans) {
  const el = document.getElementById('plan-results');
  if (!plans || !plans.length) {
    el.innerHTML = '<p class="review-empty">暂无计划数据（需先加载指数分析）</p>';
    return;
  }

  let html = '';
  plans.forEach(plan => {
    html += `<div class="plan-card">
      <div class="plan-card-header">
        <span class="plan-card-name">${plan.name}</span>
        <span class="plan-card-price">${plan.current_price}</span>
        <span class="plan-card-trend">${plan.trend}</span>
      </div>
      <div class="plan-card-structure">${plan.structure}</div>`;

    // 关键价位
    if (plan.key_levels && plan.key_levels.length) {
      html += '<div class="plan-levels">';
      plan.key_levels.forEach(lv => {
        const cls = lv.type === 'support' ? 'plan-level-support' : 'plan-level-resist';
        html += `<span class="plan-level ${cls}">${lv.name} ${lv.price}</span>`;
      });
      html += '</div>';
    }

    // 情景
    html += '<div class="plan-scenarios">';
    plan.scenarios.forEach(sc => {
      const probCls = sc.probability_hint === '偏高' ? 'prob-high' :
        sc.probability_hint === '偏低' ? 'prob-low' : 'prob-mid';
      html += `<div class="plan-scenario">
        <div class="plan-scenario-header">
          <span class="plan-scenario-name">${sc.name}</span>
          <span class="plan-scenario-prob ${probCls}">${sc.probability_hint}</span>
        </div>
        <div class="plan-scenario-trigger">触发: ${sc.trigger}</div>
        <div class="plan-scenario-action">操作: ${sc.action}</div>`;
      if (sc.target_prices && sc.target_prices.length) {
        html += `<div class="plan-scenario-targets">目标: ${sc.target_prices.join(' / ')}</div>`;
      }
      if (sc.stop_price) {
        html += `<div class="plan-scenario-stop">止损: ${sc.stop_price}</div>`;
      }
      if (sc.rationale) {
        html += `<div class="plan-scenario-rationale">${sc.rationale}</div>`;
      }
      html += '</div>';
    });
    html += '</div></div>';
  });

  el.innerHTML = html;
}

/* ── 渲染周末策略 ── */
function _renderWeekly(data) {
  const el = document.getElementById('plan-weekly');
  if (!data) {
    el.innerHTML = '';
    return;
  }

  let html = `<div class="weekly-card">
    <div class="weekly-header">${data.week_label}</div>
    <div class="weekly-outlook">${data.market_outlook}</div>
    <div class="weekly-meta">
      <span>仓位建议: <strong>${data.position_suggestion}</strong></span>`;
  if (data.style_suggestion) {
    html += `<span>风格: <strong>${data.style_suggestion}</strong></span>`;
  }
  if (data.rotation_outlook) {
    html += `<span>轮动: ${data.rotation_outlook}</span>`;
  }
  html += '</div>';

  // 板块建议
  if (data.focus_sectors.length || data.avoid_sectors.length) {
    html += '<div class="weekly-sectors">';
    if (data.focus_sectors.length) {
      html += '<div class="weekly-focus">关注: ' +
        data.focus_sectors.map(s => `<span class="weekly-tag focus">${s}</span>`).join('') +
        '</div>';
    }
    if (data.avoid_sectors.length) {
      html += '<div class="weekly-avoid">回避: ' +
        data.avoid_sectors.map(s => `<span class="weekly-tag avoid">${s}</span>`).join('') +
        '</div>';
    }
    html += '</div>';
  }

  // 宏观事件
  if (data.events && data.events.length) {
    html += '<div class="weekly-events"><h4>宏观事件</h4>';
    data.events.forEach(ev => {
      html += `<div class="weekly-event">
        <span class="weekly-event-name">${ev.event_name}</span>`;
      if (ev.event_date) {
        html += ` <span class="weekly-event-date">${ev.event_date}</span>`;
      }
      if (ev.scenarios && Object.keys(ev.scenarios).length) {
        html += '<div class="weekly-event-scenarios">';
        for (const [k, v] of Object.entries(ev.scenarios)) {
          html += `<span class="weekly-scenario-tag">${k}: ${v}</span>`;
        }
        html += '</div>';
      }
      html += '</div>';
    });
    html += '</div>';
  }

  // 关键价位
  if (data.key_levels && data.key_levels.length) {
    html += '<div class="weekly-levels"><h4>关键价位</h4><table class="review-ind-table"><thead><tr>' +
      '<th>指数</th><th>价位</th><th>名称</th><th>距离</th></tr></thead><tbody>';
    data.key_levels.forEach(lv => {
      html += `<tr><td>${lv.index}</td><td>${lv.price}</td><td>${lv.name}</td>` +
        `<td>${lv.distance_pct > 0 ? '+' : ''}${lv.distance_pct}%</td></tr>`;
    });
    html += '</tbody></table></div>';
  }

  html += '</div>';
  el.innerHTML = html;
}
