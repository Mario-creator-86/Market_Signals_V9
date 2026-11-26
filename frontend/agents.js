// Agents automation control plane. Kept separate from app.js so operational
// controls can evolve without adding more coupling to the legacy UI bundle.
(function () {
    'use strict';

    const state = {
        filter: 'all',
        search: '',
        snapshot: null,
        executionControl: null,
        loading: false,
        riskEditor: {
            open: false,
            saving: false,
            values: {},
            customValues: {},
            tradeValues: {},
            profileId: 'custom',
            error: '',
        },
    };

    const AGENTS = {
        execution: {
            icon: 'fa-bolt',
            title: 'Execution Agent',
            category: 'Trading',
            description: 'Applies confidence and execution gates before opening paper positions.',
            engineBound: true,
            masterBound: true,
            lockedWhenRunning: true,
        },
        risk: {
            icon: 'fa-shield-halved',
            title: 'Risk Manager',
            category: 'Safety',
            description: 'Enforces exposure, drawdown, position-size, and daily-loss limits.',
            engineBound: true,
            masterBound: true,
            lockedWhenRunning: true,
        },
        scanner: {
            icon: 'fa-crosshairs',
            title: 'Market Scanner',
            category: 'Discovery',
            description: 'Scans configured markets and promotes qualified opportunities.',
            engineBound: true,
        },
        sentiment: {
            icon: 'fa-comments',
            title: 'Sentiment Analysis',
            category: 'Intelligence',
            description: 'Adds news and market sentiment context to agent decisions.',
            engineBound: true,
        },
        trade_radar: {
            icon: 'fa-tower-broadcast',
            title: 'Trade Radar',
            category: 'Discovery',
            description: 'Scores and enriches momentum candidates on a scheduled cycle.',
        },
        premarket_scanner: {
            icon: 'fa-sun',
            title: 'Premarket Scanner',
            category: 'Discovery',
            description: 'Builds cache-first long, short, and watch candidates before the session.',
        },
        pre_earnings: {
            icon: 'fa-wave-square',
            title: 'Pre-Earnings',
            category: 'Intelligence',
            description: 'Monitors earnings windows, gap risk, directional bias, and calibration.',
        },
        news_tab: {
            icon: 'fa-newspaper',
            title: 'News Flow',
            category: 'Data',
            description: 'Controls background news refresh and cache maintenance.',
        },
        economic_calendar: {
            icon: 'fa-calendar-days',
            title: 'Economic Calendar',
            category: 'Data',
            description: 'Maintains economic and earnings event data through cache-first fetches.',
        },
        strategy_research: {
            icon: 'fa-flask',
            title: 'Strategy Research',
            category: 'Research',
            description: 'Builds research-only evidence. It cannot enable or authorize Capital.',
        },
        research_factory: {
            icon: 'fa-microscope',
            title: 'Research Factory',
            category: 'Research',
            description: 'Automates hypothesis discovery, walk-forward OOS validation, and strategy lifecycle promotion.',
        },
        coding_agent: {
            icon: 'fa-code',
            title: 'Coding Agent',
            category: 'Development',
            description: 'Autonomous developer investigating runtime defects and crafting minimal AST-anchored code repairs.',
        },
        critic_agent: {
            icon: 'fa-user-shield',
            title: 'Critic & Watchdog Agent',
            category: 'Development',
            description: 'Autonomous multi-stage reviewer (Syntax AST, Protected Paths, Test Suites) guarding all coding repairs.',
        },
        ui_inspector: {
            icon: 'fa-eye',
            title: 'UI & Data Health Inspector',
            category: 'Maintenance',
            description: 'Continuously monitors all UI tabs for stale state, missing validation data, empty news, or visual defects and automatically reports anomalies to Coding Agent for self-repair.',
        },
    };

    const BENIGN_PAUSES = new Set(['market_open_paused', 'outside_schedule', 'market_closed']);

    function escapeHtml(value) {
        return String(value == null ? '' : value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#039;');
    }

    function sentenceCase(value) {
        const text = String(value == null ? '' : value).trim().replaceAll('_', ' ');
        return text ? text.charAt(0).toUpperCase() + text.slice(1) : '';
    }

    // Status chip in the design-system recipe; tone is pos / neg / warn / info or none.
    function chip(label, tone) {
        return `<span class="ds-chip${tone ? ` ds-chip--${tone}` : ''}">${escapeHtml(label)}</span>`;
    }

    function scrollToSection(id) {
        return `document.getElementById('${id}')?.scrollIntoView({ block: 'start' })`;
    }

    function asEnabled(value) {
        return typeof value === 'object' && value !== null
            ? value.enabled === true
            : value === true;
    }

    function dateMs(value) {
        const parsed = value ? new Date(value).getTime() : NaN;
        return Number.isFinite(parsed) ? parsed : null;
    }

    function relativeTime(value) {
        const parsed = dateMs(value);
        if (parsed === null) return 'No run recorded';
        const seconds = Math.max(0, Math.round((Date.now() - parsed) / 1000));
        if (seconds < 10) return 'Just now';
        if (seconds < 60) return `${seconds}s ago`;
        const minutes = Math.round(seconds / 60);
        if (minutes < 60) return `${minutes}m ago`;
        const hours = Math.round(minutes / 60);
        if (hours < 48) return `${hours}h ago`;
        return `${Math.round(hours / 24)}d ago`;
    }

    function durationLabel(value) {
        const ms = Number(value);
        if (!Number.isFinite(ms) || ms < 0) return 'Not measured';
        if (ms < 1000) return `${Math.round(ms)}ms`;
        if (ms < 60000) return `${(ms / 1000).toFixed(ms < 10000 ? 1 : 0)}s`;
        return `${(ms / 60000).toFixed(1)}m`;
    }

    function nextRunLabel(value) {
        const seconds = Number(value);
        if (!Number.isFinite(seconds)) return 'On demand';
        if (seconds <= 0) return 'Due now';
        if (seconds < 60) return `${Math.round(seconds)}s`;
        return `${Math.round(seconds / 60)}m`;
    }

    function intervalLabel(value) {
        const seconds = Number(value);
        if (!Number.isFinite(seconds) || seconds <= 0) return 'Not scheduled';
        if (seconds < 120) return `Every ${Math.round(seconds)}s`;
        if (seconds < 7200) return `Every ${Math.round(seconds / 60)}m`;
        return `Every ${Math.round(seconds / 3600)}h`;
    }

    function lastActivityLabel(runtime) {
        const value = runtime?.last_activity_at || runtime?.last_success || runtime?.last_run || runtime?.last_heartbeat;
        return value ? relativeTime(value) : 'No run recorded';
    }

    function configDetail(key, config, agentConfig, runtime = {}) {
        if (key === 'execution') {
            const mode = sentenceCase(String(agentConfig.mode || config.bot?.mode || 'paper').toLowerCase());
            return { left: 'Mode', leftValue: mode, right: 'Max open trades', rightValue: agentConfig.max_open_trades ?? '—' };
        }
        if (key === 'risk') {
            const constitution = config.risk_constitution || {};
            const active = constitution.active || null;
            if (active) {
                return {
                    left: 'Active daily limit',
                    leftValue: `${active.max_daily_loss_pct}%`,
                    right: 'Active risk / trade',
                    rightValue: `${active.max_risk_per_trade_pct}%`,
                };
            }
            const status = constitution.status === 'draft' ? 'Draft saved' : 'Not active';
            return { left: 'Risk Constitution', leftValue: status, right: 'Owner settings', rightValue: 'Open editor' };
        }
        if (key === 'scanner') {
            return { left: 'Interval', leftValue: `${agentConfig.interval_sec || 60}s`, right: 'Min volume', rightValue: formatCompact(agentConfig.min_volume_24h) };
        }
        if (key === 'sentiment') {
            // The worker aggregates the sentiment labels News Flow already put on
            // market articles. `provider` / `min_sentiment_score` in its config are
            // read by no code, so showing "marketaux · 0.2" described a provider
            // that is neither called nor configured.
            const refresh = Math.max(60, Number(agentConfig.refresh_seconds) || 600);
            return { left: 'Source', leftValue: 'News Flow labels', right: 'Refresh', rightValue: `${refresh}s` };
        }
        if (key === 'premarket_scanner') {
            return { left: 'Refresh', leftValue: `${config.premarket?.refresh_seconds || 300}s`, right: 'Cache TTL', rightValue: `${config.premarket?.cache_ttl_seconds || 300}s` };
        }
        if (key === 'pre_earnings') {
            return { left: 'Refresh', leftValue: `${config.pre_earnings?.refresh_seconds || 600}s`, right: 'Lookahead', rightValue: `${config.pre_earnings?.lookahead_days || 7}d` };
        }
        if (key === 'news_tab') {
            return { left: 'Refresh', leftValue: `${config.news?.news_refresh_minutes || 5}m`, right: 'Mode', rightValue: 'Cache-first' };
        }
        if (key === 'strategy_research') {
            return { left: 'Interval', leftValue: `${config.strategy_v2?.research_agent?.interval_sec || 900}s`, right: 'Mode', rightValue: 'Research only' };
        }
        if (key === 'ui_inspector') {
            const open = Number(runtime.unresolved_anomalies);
            return {
                left: 'Tabs checked',
                leftValue: Number.isFinite(Number(runtime.tabs_checked)) ? String(runtime.tabs_checked) : 'No scan yet',
                right: 'Open anomalies',
                rightValue: Number.isFinite(open) ? String(open) : 'No scan yet',
            };
        }
        if (key === 'coding_agent' || key === 'critic_agent') {
            const cycles = Number(runtime.cycles_completed);
            return {
                left: 'Watchdog cycles',
                leftValue: Number.isFinite(cycles) ? String(cycles) : 'None recorded',
                right: 'Last cycle',
                rightValue: lastActivityLabel(runtime),
            };
        }
        // No invented "On demand / Cache-first": show what the worker reported.
        return {
            left: 'Schedule',
            leftValue: runtime.schedule_interval_seconds != null
                ? intervalLabel(runtime.schedule_interval_seconds)
                : (runtime.running === true ? 'Background worker' : 'Not running'),
            right: 'Last success',
            rightValue: runtime.last_success ? relativeTime(runtime.last_success) : 'None recorded',
        };
    }

    function riskFieldTestId(name) {
        if (name === 'max_daily_loss_pct') return 'risk-daily-loss-input';
        if (name === 'max_daily_loss_trades') return 'risk-daily-loss-trades-input';
        if (name === 'max_risk_per_trade_pct') return 'risk-per-trade-input';
        return `risk-field-${name}`;
    }

    function riskStatusCopy(status) {
        const statuses = {
            active: ['Active', 'These owner-confirmed limits currently veto new entries.'],
            draft: ['Draft saved', 'No Risk Constitution is active yet.'],
            draft_changes: ['Active + draft changes', 'Active limits remain unchanged until you confirm this draft.'],
            invalid_active: ['Needs attention', 'The active configuration is invalid and must be replaced.'],
            unconfigured: ['Not configured', 'Choose every value yourself; no preset is inserted.'],
        };
        return statuses[status] || statuses.unconfigured;
    }

    function riskFieldHtml(field, value, scope = 'constitution') {
        const name = String(field.name || '');
        const id = `risk-${scope}-${name.replaceAll('_', '-')}`;
        const testId = scope === 'constitution' ? riskFieldTestId(name) : `risk-trade-field-${name}`;
        const renderedValue = value === null || value === undefined ? '' : String(value);
        const help = field.help ? `<small class="ag-field-help">${escapeHtml(field.help)}</small>` : '';
        if (field.kind === 'boolean') {
            return `
                <label class="ag-field" for="${escapeHtml(id)}">
                    <span class="ag-field-label">${escapeHtml(field.label)}</span>
                    <span class="ag-field-control">
                        <select id="${escapeHtml(id)}" name="${escapeHtml(name)}"
                            data-testid="${escapeHtml(testId)}" data-risk-field="${escapeHtml(name)}"
                            onchange="Actions.updateRiskSettingsDraft(this.form)">
                            <option value="" ${renderedValue === '' ? 'selected' : ''}>Not set</option>
                            <option value="true" ${renderedValue === 'true' ? 'selected' : ''}>Yes</option>
                            <option value="false" ${renderedValue === 'false' ? 'selected' : ''}>No</option>
                        </select>
                    </span>
                    ${help}
                </label>`;
        }
        if (field.kind === 'enum') {
            const options = Array.isArray(field.options) ? field.options : [];
            return `
                <label class="ag-field" for="${escapeHtml(id)}">
                    <span class="ag-field-label">${escapeHtml(field.label)}</span>
                    <span class="ag-field-control">
                        <select id="${escapeHtml(id)}" name="${escapeHtml(name)}"
                            data-testid="${escapeHtml(testId)}" data-risk-field="${escapeHtml(name)}"
                            onchange="Actions.updateRiskSettingsDraft(this.form)">
                            <option value="" ${renderedValue === '' ? 'selected' : ''}>Not set</option>
                            ${options.map((option) => `
                                <option value="${escapeHtml(option)}" ${renderedValue === String(option) ? 'selected' : ''}>${escapeHtml(option)}</option>`).join('')}
                        </select>
                    </span>
                    ${help}
                </label>`;
        }
        const minAttr = field.minimum === null || field.minimum === undefined ? '' : ` min="${escapeHtml(field.minimum)}"`;
        const maxAttr = field.maximum === null || field.maximum === undefined ? '' : ` max="${escapeHtml(field.maximum)}"`;
        const stepAttr = field.step === null || field.step === undefined ? '' : ` step="${escapeHtml(field.step)}"`;
        return `
            <label class="ag-field" for="${escapeHtml(id)}">
                <span class="ag-field-label">${escapeHtml(field.label)}</span>
                <span class="ag-field-control">
                    <input id="${escapeHtml(id)}" name="${escapeHtml(name)}" type="number"
                        inputmode="decimal"${minAttr}${maxAttr}${stepAttr}
                        value="${escapeHtml(renderedValue)}" data-testid="${escapeHtml(testId)}"
                        data-risk-field="${escapeHtml(name)}" oninput="Actions.updateRiskSettingsDraft(this.form)">
                    ${field.unit ? `<span class="ag-field-unit" aria-hidden="true">${escapeHtml(field.unit)}</span>` : ''}
                </span>
                ${help}
            </label>`;
    }

    function riskProfileItems(constitution) {
        const items = constitution?.profiles?.items;
        return Array.isArray(items) ? items.filter((profile) => profile && typeof profile.values === 'object') : [];
    }

    function riskValuesEqual(left, right, schema) {
        return schema.every((field) => left?.[field.name] === right?.[field.name]);
    }

    function riskProfileSummary(profile) {
        const values = profile?.values || {};
        return [
            `${values.max_risk_per_trade_pct}% risk/trade`,
            `${values.max_total_exposure_pct}% exposure`,
            `${values.max_leverage}x leverage`,
        ];
    }

    function riskProfilesHtml(constitution, editor) {
        const profiles = riskProfileItems(constitution);
        const profilesState = constitution?.profiles || {};
        const profileCards = profiles.map((profile) => {
            const active = editor.profileId === profile.id;
            return `
                <button type="button" class="ag-profile ${active ? 'is-selected' : ''}"
                    data-risk-profile-id="${escapeHtml(profile.id)}" data-testid="risk-profile-${escapeHtml(profile.id)}"
                    aria-pressed="${active ? 'true' : 'false'}"
                    onclick="Actions.selectRiskConstitutionProfile(this.dataset.riskProfileId)">
                    <span class="agents-risk-profile-head ag-profile-head">
                        <strong>${escapeHtml(profile.label)}</strong>
                        <span>${active ? 'Selected' : 'Use profile'}</span>
                    </span>
                    <small>${escapeHtml(profile.description)}</small>
                    <span class="ag-profile-metrics">
                        ${riskProfileSummary(profile).map((item) => `<span>${escapeHtml(item)}</span>`).join('')}
                    </span>
                </button>`;
        }).join('');
        const customActive = editor.profileId === 'custom';
        const error = profilesState.status === 'error'
            ? `<p class="ag-risk-error" role="alert">Profiles unavailable: ${escapeHtml(profilesState.error || 'configuration error')}</p>`
            : '';
        return `
            <section class="ag-dialog-section" aria-labelledby="risk-profiles-title" data-testid="risk-profile-picker">
                <div class="ag-dialog-section-head">
                    <h4 id="risk-profiles-title">Choose a paper trading profile or build your own</h4>
                    <span>${escapeHtml(profilesState.source || 'profile configuration')}</span>
                </div>
                <div class="ag-profile-grid">
                    ${profileCards}
                    <button type="button" class="ag-profile ${customActive ? 'is-selected' : ''}"
                        data-risk-profile-id="custom" data-testid="risk-profile-custom"
                        aria-pressed="${customActive ? 'true' : 'false'}"
                        onclick="Actions.selectRiskConstitutionProfile('custom')">
                        <span class="agents-risk-profile-head ag-profile-head">
                            <strong>Custom</strong>
                            <span>${customActive ? 'Selected' : 'Edit freely'}</span>
                        </span>
                        <small>Use your saved draft and adjust every limit directly. No value is inserted automatically.</small>
                        <span class="ag-profile-metrics"><span>Owner-defined</span><span>Fully editable</span></span>
                    </button>
                </div>
                ${error}
                <p class="ag-dialog-help">Selecting a profile only fills this form. It does not save or activate the Constitution.</p>
            </section>`;
    }

    function riskEditorHtml(constitution) {
        const editor = state.riskEditor;
        const schema = Array.isArray(constitution?.schema) ? constitution.schema : [];
        const tradePolicy = constitution?.trade_policy || {};
        const tradeSchema = Array.isArray(tradePolicy.schema) ? tradePolicy.schema : [];
        const groupSchema = (fields, fallback) => {
            const groups = [];
            fields.forEach((field) => {
                const sectionName = String(field.section || fallback);
                let section = groups.find((item) => item.name === sectionName);
                if (!section) {
                    section = { name: sectionName, fields: [] };
                    groups.push(section);
                }
                section.fields.push(field);
            });
            return groups;
        };
        const sections = groupSchema(schema, 'Portfolio limits');
        const tradeSections = groupSchema(tradeSchema, 'Trade policy');
        const status = riskStatusCopy(constitution?.status);
        const statusTone = riskStatusTone(constitution?.status);
        const activeFingerprint = constitution?.manager_fingerprint || constitution?.fingerprint;
        const fingerprintLabel = activeFingerprint
            ? `Policy ${String(activeFingerprint).slice(0, 10)}`
            : 'No active policy';
        const control = state.executionControl || {};
        const readiness = control.readiness || {};
        const reconciliation = control.reconciliation || {};
        const stopped = control.kill_switch_active === true;
        const drift = Array.isArray(tradePolicy.runtime_drift) ? tradePolicy.runtime_drift : [];
        const reconciliationStatus = String(reconciliation.status || 'UNVERIFIED');
        const fieldSections = (groups, values, scope) => groups.map((section) => `
                                <section class="ag-field-section" aria-label="${escapeHtml(section.name)}">
                                    <h5>${escapeHtml(section.name)}</h5>
                                    <div class="ag-field-grid">
                                        ${section.fields.map((field) => riskFieldHtml(field, values[field.name], scope)).join('')}
                                    </div>
                                </section>`).join('');
        return `
            <div id="risk-settings-modal" class="ag-modal" role="presentation"
                onclick="Actions.handleRiskSettingsBackdrop(event)">
                <form id="risk-settings-form" class="ag-dialog" role="dialog" aria-modal="true"
                    aria-labelledby="risk-settings-title" onsubmit="Actions.saveRiskSettings(event, this, true)" novalidate
                    data-testid="risk-settings-form">
                    <header class="ag-dialog-head">
                        <div>
                            <h3 id="risk-settings-title">Risk Manager</h3>
                            <p>The single policy authority for paper trading: portfolio limits, trade admission, sizing and exit policy are saved together here.</p>
                        </div>
                        <button type="button" class="ds-btn ds-btn--ghost ag-dialog-close" aria-label="Close Risk Manager editor"
                            data-testid="risk-settings-cancel" onclick="Actions.closeRiskSettings()" ${editor.saving ? 'disabled' : ''}>
                            <i class="fa-solid fa-xmark" aria-hidden="true"></i>
                        </button>
                    </header>
                    <div class="ag-dialog-body">
                        <div class="ag-risk-status">
                            ${chip(status[0], statusTone)}
                            <span class="ag-risk-fingerprint">${escapeHtml(fingerprintLabel)}</span>
                            <span class="ag-risk-status-copy">${escapeHtml(status[1])}</span>
                        </div>
                        ${drift.length ? `<p class="ag-risk-error" role="alert">Runtime mirror drift detected: ${escapeHtml(drift.join(', '))}. Confirm and apply to repair it atomically.</p>` : ''}
                        ${riskProfilesHtml(constitution, editor)}
                        <section class="ag-dialog-section" aria-labelledby="risk-constitution-block-title">
                            <div class="ag-dialog-section-head">
                                <h4 id="risk-constitution-block-title">Portfolio Constitution</h4>
                                <span>Hard exposure and loss vetoes</span>
                            </div>
                            ${fieldSections(sections, editor.values, 'constitution')}
                        </section>
                        <section class="ag-dialog-section" aria-labelledby="risk-trade-policy-block-title" data-testid="risk-trade-policy">
                            <div class="ag-dialog-section-head">
                                <h4 id="risk-trade-policy-block-title">Trade admission, sizing & exits</h4>
                                <span>Unified execution controls, owned only by Risk Manager</span>
                            </div>
                            ${fieldSections(tradeSections, editor.tradeValues, 'trade')}
                        </section>
                        <section class="ag-dialog-section ag-risk-operations" aria-labelledby="risk-operations-title">
                            <div class="ag-dialog-section-head">
                                <h4 id="risk-operations-title">Risk Manager pipeline</h4>
                                <span>Live operations</span>
                            </div>
                            <div class="ag-risk-operations-row">
                                <div class="ag-risk-operations-state">
                                    <p>${readiness.ready ? 'Ready to admit qualified paper entries.' : escapeHtml((readiness.reasons || []).join(' · ') || 'Readiness is being verified.')}</p>
                                    <p class="ag-risk-operations-meta">
                                        <span>Broker reconciliation ${chip(sentenceCase(reconciliationStatus.toLowerCase()), reconciliationStatus === 'HEALTHY' ? 'pos' : 'warn')}</span>
                                        <span>New-entry stop ${chip(stopped ? 'Active' : 'Released', stopped ? 'warn' : '')}</span>
                                    </p>
                                </div>
                                <div class="ag-risk-operation-actions">
                                    <button type="button" class="ds-btn ${stopped ? '' : 'ds-btn--danger'}"
                                        data-testid="risk-manager-kill-switch" onclick="Actions.setPaperKillSwitch(${stopped ? 'false' : 'true'})">
                                        <i class="fa-solid ${stopped ? 'fa-play' : 'fa-stop'}" aria-hidden="true"></i>${stopped ? 'Resume new entries' : 'Stop new entries'}
                                    </button>
                                    <button type="button" class="ds-btn" data-testid="risk-manager-run-cycle"
                                        onclick="Actions.runRiskManagerCycle()"><i class="fa-solid fa-play" aria-hidden="true"></i> Run analysis now</button>
                                    <button type="button" class="ds-btn" data-testid="risk-manager-reconcile"
                                        onclick="Actions.reconcilePaperExecution()"><i class="fa-solid fa-scale-balanced" aria-hidden="true"></i> Reconcile broker</button>
                                </div>
                            </div>
                        </section>
                        <p class="ag-dialog-help" data-testid="risk-settings-hint">
                            <strong data-testid="risk-settings-completion">Checking selected values...</strong> Save draft keeps the active policy unchanged. Confirm and apply updates every Risk Manager rule and all runtime mirrors atomically.
                        </p>
                        <p class="ag-risk-error ${editor.error ? '' : 'hidden'}" role="alert"
                            data-testid="risk-settings-error">${escapeHtml(editor.error)}</p>
                    </div>
                    <div class="ag-dialog-foot">
                        <button class="ds-btn" type="button"
                            onclick="Actions.closeRiskSettings()" ${editor.saving ? 'disabled' : ''}>Cancel</button>
                        <button class="ds-btn" type="button"
                            data-testid="risk-settings-save-draft" onclick="Actions.saveRiskSettings(event, this.form, false)"
                            ${editor.saving ? 'disabled' : ''}>Save draft</button>
                        <button class="ds-btn ds-btn--primary" type="submit"
                            data-testid="risk-settings-save" ${editor.saving ? 'disabled' : ''}>
                            ${editor.saving ? '<i class="fa-solid fa-spinner fa-spin"></i> Saving...' : '<i class="fa-solid fa-shield"></i> Confirm and apply'}
                        </button>
                    </div>
                </form>
            </div>`;
    }

    function formatCompact(value) {
        const number = Number(value);
        if (!Number.isFinite(number)) return '—';
        return new Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 1 }).format(number);
    }

    function computeAgentState(key, enabled, runtime, bot) {
        if (!enabled) return { key: 'disabled', label: 'Disabled', group: 'disabled' };

        const error = String(runtime.last_error || '').trim().toLowerCase();
        const lifecycle = String(runtime.lifecycle_state || '').trim().toLowerCase();
        if (error && BENIGN_PAUSES.has(error)) {
            if (runtime.running === true) {
                return { key: 'running', label: 'Running / scheduled', group: 'running' };
            }
            return { key: 'paused', label: 'Schedule paused', group: 'attention' };
        }
        if (lifecycle === 'error' || error) {
            return { key: 'error', label: 'Error', group: 'attention' };
        }
        if (runtime.initializing === true || lifecycle === 'starting') {
            return { key: 'starting', label: 'Starting…', group: 'running' };
        }
        // The worker thread is alive but its latest cycle raised: "Running" would hide
        // that nothing useful came out of the last run.
        if (runtime.last_run_ok === false) {
            return { key: 'error', label: 'Last run failed', group: 'attention' };
        }

        const meta = AGENTS[key];
        if (runtime.running === true && runtime.overdue === true) {
            // Thread alive, but no completed cycle / heartbeat for several intervals.
            return { key: 'attention', label: 'Running / overdue', group: 'attention' };
        }
        if (runtime.running === true) {
            if (meta.masterBound && runtime.accepting_new_entries === false) {
                // Bot on but a gate (named on the master card) refuses new risk:
                // never label that "entries enabled".
                return bot.running === true
                    ? { key: 'running', label: 'Running / entries blocked', group: 'running' }
                    : { key: 'running', label: 'Running / monitoring only', group: 'running' };
            }
            if (meta.masterBound && runtime.accepting_new_entries === true) {
                return { key: 'running', label: 'Running / entries enabled', group: 'running' };
            }
            if (meta.engineBound) {
                return { key: 'running', label: 'Running / engine', group: 'running' };
            }
            if (runtime.next_run_in != null) {
                return { key: 'running', label: 'Running / scheduled', group: 'running' };
            }
            return { key: 'running', label: 'Running', group: 'running' };
        }
        if (meta.engineBound) {
            if (bot.engine_running === false) {
                return { key: 'attention', label: 'Engine offline', group: 'attention' };
            }
        }
        if (runtime.ready === true && lifecycle === 'test_suppressed') {
            return { key: 'standby', label: 'Ready (test mode)', group: 'attention' };
        }
        return { key: 'attention', label: 'Stopped / attention', group: 'attention' };
    }

    // Single source of truth for "can a new paper entry open right now".
    // The server sends entry_gate (the same readiness the PaperBroker enforces);
    // the fallback rebuilds it from the legacy fields for an older server.
    function resolveEntryGate(bot, config, executionControl) {
        const served = state.snapshot?.entryGate;
        if (served && Array.isArray(served.gates)) return served;
        const readiness = executionControl?.readiness || {};
        const reasons = Array.isArray(readiness.reasons) ? readiness.reasons : [];
        const policyReady = readiness.ready === true;
        const gates = [
            { key: 'market_engine', label: 'Market engine', ready: bot.engine_running !== false, detail: 'The market engine process is not running', action: 'Start the market engine.' },
            { key: 'master_switch', label: 'Bot master switch', ready: bot.running === true, detail: 'The master switch is off', action: 'Press Start bot.' },
            { key: 'execution_agent', label: 'Execution Agent', ready: asEnabled(config.agents?.execution), detail: 'Execution Agent is disabled', action: 'Turn on the Execution Agent switch.' },
            { key: 'risk_manager', label: 'Risk Manager', ready: asEnabled(config.agents?.risk), detail: 'Risk Manager is disabled', action: 'Turn on the Risk Manager switch.' },
            { key: 'candidate_worker', label: 'Internal candidate worker', ready: config.agents?.execution?.autonomous_candidates_enabled === true, detail: 'Autonomous candidates are off', action: 'There is no switch for this in the app: set agents.execution.autonomous_candidates_enabled to true in config.json.' },
            { key: 'circuit_breaker', label: 'Circuit breaker', ready: bot.circuit_breaker_active !== true, detail: 'Daily loss / drawdown breaker tripped today', action: 'It re-arms on the next trading day.' },
            { key: 'paper_mode', label: 'Paper mode', ready: String(bot.mode || config.bot?.mode || 'paper').toLowerCase() === 'paper', detail: 'Bot mode is not paper', action: 'Set the bot mode to paper.' },
            { key: 'risk_policy', label: 'Risk Manager active policy', ready: policyReady, detail: reasons.join(' · ') || 'Paper-entry readiness could not be verified', action: 'Open Risk Manager and resolve the listed item.' },
        ];
        const blockers = gates.filter((gate) => !gate.ready);
        return {
            open: blockers.length === 0,
            status: blockers.length === 0 ? 'open' : 'blocked',
            ready_count: gates.length - blockers.length,
            total: gates.length,
            gates,
            blockers,
        };
    }

    // Where the action that clears a blocked gate is performed.
    const RISK_MANAGER_GATES = new Set(['broker_reconciliation', 'risk_constitution', 'risk_policy', 'kill_switch', 'execution_mode', 'reduce_fraction']);
    const FLEET_GATES = new Set(['execution_agent', 'risk_manager']);

    function blockerLinkHtml(gate) {
        if (RISK_MANAGER_GATES.has(gate.key)) {
            return '<button type="button" class="ds-btn ag-blocker-link" data-testid="agents-blocker-action" onclick="Actions.toggleRiskSettings()">Open Risk Manager</button>';
        }
        if (FLEET_GATES.has(gate.key)) {
            return `<button type="button" class="ds-btn ag-blocker-link" data-testid="agents-blocker-action" onclick="${scrollToSection('agents-catalog-title')}">Go to agent fleet</button>`;
        }
        return `<button type="button" class="ds-btn ag-blocker-link" data-testid="agents-blocker-action" onclick="${scrollToSection('agents-readiness-panel')}">Show readiness gates</button>`;
    }

    function renderMaster(bot, config) {
        const panel = document.getElementById('agents-master-panel');
        if (!panel) return;
        const running = bot.running === true;
        const engineRunning = bot.engine_running !== false;
        const executionEnabled = asEnabled(config.agents?.execution);
        const riskEnabled = asEnabled(config.agents?.risk);
        const gate = resolveEntryGate(bot, config, state.executionControl);
        const entryBlockers = gate.blockers.filter((item) => item.key !== 'master_switch');
        const firstBlocker = entryBlockers[0] || null;
        const startBlocked = !engineRunning || !executionEnabled || !riskEnabled;
        const copy = running && gate.open
            ? 'The master switch is on. Qualified signals may pass through risk, confidence, and execution gates and open paper positions.'
            : running
                ? `The bot is monitoring and evaluating candidates, but no new paper position can open: ${entryBlockers.length} gate(s) blocked.`
            : 'The master switch is off. Agents may continue collecting and analyzing data, but no new position can be opened automatically.';
        const blockedReason = !engineRunning
            ? 'Start the market engine before enabling automation.'
            : (!executionEnabled || !riskEnabled)
                ? 'Execution Agent and Risk Manager must both be enabled.'
                : 'Individual agent switches cannot bypass this master control.';
        const pillLabel = !running
            ? 'Bot stopped'
            : (gate.open ? 'Bot running · entries enabled' : 'Bot running · entries blocked');
        const stateTone = !running ? 'idle' : (gate.open ? 'pos' : 'warn');
        const modeLabel = sentenceCase(String(bot.mode || config.bot?.mode || 'paper').toLowerCase());
        const blockerHtml = firstBlocker
            ? `<div class="ag-blocker" role="status" data-testid="agents-entry-blocker">
                    <i class="fa-solid fa-triangle-exclamation" aria-hidden="true"></i>
                    <div class="ag-blocker-text">
                        <p><strong>Blocked by ${escapeHtml(firstBlocker.label)}:</strong> ${escapeHtml(firstBlocker.detail || 'not ready')}.</p>
                        ${firstBlocker.action ? `<p><strong>To clear it:</strong> ${escapeHtml(firstBlocker.action)}</p>` : ''}
                        ${entryBlockers.length > 1 ? `<p>${entryBlockers.length - 1} more blocked gate(s) are listed under Automation readiness.</p>` : ''}
                    </div>
                    ${blockerLinkHtml(firstBlocker)}
               </div>`
            : '';

        panel.className = `ag-master ${running ? 'is-running' : 'is-stopped'}`;
        panel.innerHTML = `
            <div class="ag-master-main">
                <div class="ag-master-name">
                    <h3>Risk-managed trading bot</h3>
                    <span class="ds-chip">${escapeHtml(modeLabel)} mode</span>
                </div>
                <div class="ag-master-state">
                    <span class="ag-state ag-state--${stateTone}" data-testid="agents-bot-status">${escapeHtml(pillLabel)}</span>
                    <p class="ag-master-copy">${escapeHtml(copy)}</p>
                </div>
            </div>
            <div class="ag-master-actions">
                <div class="ag-master-buttons">
                    <button class="ds-btn ds-btn--primary" type="button" data-testid="agents-bot-start"
                        onclick="Actions.setBotRunning(true)" ${running || startBlocked ? 'disabled' : ''}>
                        <i class="fa-solid fa-play" aria-hidden="true"></i> Start bot
                    </button>
                    <button class="ds-btn ds-btn--danger" type="button" data-testid="agents-bot-stop"
                        onclick="Actions.setBotRunning(false)" ${!running ? 'disabled' : ''}>
                        <i class="fa-solid fa-stop" aria-hidden="true"></i> Stop bot
                    </button>
                </div>
                <p class="ag-master-note">${escapeHtml(blockedReason)}</p>
            </div>
            ${blockerHtml}`;
    }

    function renderKpis(bot, cards) {
        const grid = document.getElementById('agents-kpi-grid');
        if (!grid) return;
        const runningCount = cards.filter((card) => card.status.group === 'running').length;
        const enabledCount = cards.filter((card) => card.enabled).length;
        const attentionCount = cards.filter((card) => card.enabled && card.status.group === 'attention').length;
        const breaker = bot.circuit_breaker_active === true;
        // The breaker thread only watches equity while the bot controller runs
        // (core/bot_controller._circuit_breaker_loop); "Ready" with the bot
        // stopped claimed a guard that was not watching anything.
        const breakerArmed = bot.running === true && bot.controller_running !== false;
        const breakerLimits = state.snapshot?.config?.bot || {};
        const dailyLimit = Number(breakerLimits.circuit_breaker_daily_loss_pct);
        const drawdownLimit = Number(breakerLimits.circuit_breaker_drawdown_pct);
        const breakerNote = Number.isFinite(dailyLimit) && Number.isFinite(drawdownLimit)
            ? `Trips at ${dailyLimit}% daily loss or ${drawdownLimit}% drawdown of the day-start equity`
            : 'Daily loss / drawdown limits unavailable';
        const threshold = Number(bot.min_edge_score);
        const engineOffline = bot.engine_running === false;
        const items = [
            {
                label: 'Market engine',
                value: engineOffline ? 'Offline' : 'Running',
                tone: engineOffline ? 'neg' : 'pos',
                note: engineOffline ? 'Start the market engine to resume automation' : 'Engine process is running',
            },
            {
                label: 'Worker fleet',
                value: `${runningCount} / ${enabledCount}`,
                unit: 'healthy / enabled',
                tone: attentionCount ? 'warn' : (runningCount ? 'pos' : 'warn'),
                note: attentionCount ? `${attentionCount} enabled agent(s) need attention` : 'Every enabled agent reported in',
            },
            {
                label: 'Circuit breaker',
                value: breaker ? 'Triggered' : (breakerArmed ? 'Armed' : 'Standby'),
                tone: breaker ? 'neg' : (breakerArmed ? 'pos' : 'warn'),
                note: breaker ? 'New entries stay blocked until the next trading day' : (breakerArmed ? breakerNote : `Not monitoring while the bot is stopped. ${breakerNote}`),
            },
            {
                label: 'Entry threshold',
                value: Number.isFinite(threshold) ? `${threshold.toFixed(0)} / 100` : 'Unavailable',
                tone: '',
                note: 'Minimum signal score required before an entry is considered',
            },
        ];
        grid.innerHTML = items.map((item) => `
            <div class="ds-stat">
                <div class="ds-label">${item.label}</div>
                <div class="ds-stat-value ag-stat-value">${item.tone ? `<span class="ag-dot ag-dot--${item.tone}" aria-hidden="true"></span>` : ''}${escapeHtml(item.value)}${item.unit ? ` <span class="ag-stat-unit">${escapeHtml(item.unit)}</span>` : ''}</div>
                ${item.note ? `<div class="ds-stat-note">${escapeHtml(item.note)}</div>` : ''}
            </div>`).join('');
    }

    function renderReadiness(bot, config, executionControl = {}) {
        const panel = document.getElementById('agents-readiness-panel');
        if (!panel) return;
        // Same gate list the master card and the Execution/Risk cards read, so a
        // failing gate is named here together with what clears it.
        const gate = resolveEntryGate(bot, config, executionControl);
        const checks = gate.gates;
        const readyCount = checks.filter((check) => check.ready).length;
        // Only the served gate list words `detail` for a passing gate; the
        // fallback list describes the failure, which must not sit beside "Ready".
        const servedGates = Boolean(state.snapshot?.entryGate);
        const ordered = [...checks.filter((check) => !check.ready), ...checks.filter((check) => check.ready)];
        panel.className = 'ag-panel ag-readiness';
        panel.innerHTML = `
            <div class="ag-panel-head">
                <div>
                    <h3>Automation readiness</h3>
                    <p>Every gate a new paper entry must pass. ${gate.open ? 'All passed.' : 'Blocked gates are listed first with the action that clears them.'}</p>
                </div>
                <span class="ds-chip ${gate.open ? 'ds-chip--pos' : 'ds-chip--warn'}" data-testid="agents-readiness-score">${readyCount}/${checks.length} ready</span>
            </div>
            <div class="ag-table-wrap">
                <table class="ag-table ag-readiness-table">
                    <thead>
                        <tr><th scope="col">Gate</th><th scope="col">State</th><th scope="col">Detail</th><th scope="col">Action</th></tr>
                    </thead>
                    <tbody>
                        ${ordered.map((check) => `
                        <tr class="${check.ready ? 'is-ready' : 'is-blocked'}" data-gate="${escapeHtml(check.key || '')}">
                            <th scope="row">${escapeHtml(check.label)}</th>
                            <td>${chip(check.ready ? 'Ready' : 'Blocked', check.ready ? 'pos' : 'warn')}</td>
                            <td class="ag-cell-detail">${escapeHtml(check.ready ? (servedGates && check.detail ? check.detail : 'Ready') : (check.detail || 'Blocked'))}</td>
                            <td class="ag-cell-action">${check.ready || !check.action ? '<span class="ag-muted">—</span>' : escapeHtml(check.action)}</td>
                        </tr>`).join('')}
                    </tbody>
                </table>
            </div>`;
    }

    function riskStatusTone(status) {
        return { active: 'pos', draft: 'warn', draft_changes: 'warn', invalid_active: 'neg' }[status] || '';
    }

    function riskPolicyValue(field, value) {
        if (value === null || value === undefined || value === '') return { text: '—', unit: '' };
        if (field.kind === 'boolean' || typeof value === 'boolean') return { text: value === true ? 'Yes' : 'No', unit: '' };
        const unit = String(field.unit || '');
        if (unit === '%' || unit === '×') return { text: `${value}${unit}`, unit: '' };
        if (unit === 'ratio') return { text: String(value), unit: '' };
        if (unit === 'seconds') return { text: String(value), unit: 's' };
        return { text: String(value), unit };
    }

    // The limits the Risk Manager enforces right now (the owner-confirmed
    // Constitution), read from the same snapshot as the editor.
    function renderRiskPolicy(config) {
        const panel = document.getElementById('agents-risk-policy');
        if (!panel) return;
        const constitution = config.risk_constitution || {};
        const active = constitution.active && typeof constitution.active === 'object' ? constitution.active : null;
        const schema = Array.isArray(constitution.schema) && constitution.schema.length
            ? constitution.schema
            : Object.keys(active || {}).filter((name) => name !== 'owner_confirmed')
                .map((name) => ({ name, label: sentenceCase(name), section: 'Active limits' }));
        const status = riskStatusCopy(constitution.status);
        const fingerprint = constitution.manager_fingerprint || constitution.fingerprint;
        const sections = [];
        schema.forEach((field) => {
            const name = String(field.section || 'Active limits');
            let section = sections.find((item) => item.name === name);
            if (!section) {
                section = { name, fields: [] };
                sections.push(section);
            }
            section.fields.push(field);
        });
        const body = active && sections.length
            ? `<div class="ag-policy-grid">
                    ${sections.map((section) => `
                    <section class="ag-policy-section" aria-label="${escapeHtml(section.name)}">
                        <h4>${escapeHtml(section.name)}</h4>
                        <dl class="ag-dl">
                            ${section.fields.map((field) => {
                                const value = riskPolicyValue(field, active[field.name]);
                                return `<div><dt>${escapeHtml(field.label)}</dt><dd>${escapeHtml(value.text)}${value.unit ? ` <span class="ag-unit">${escapeHtml(value.unit)}</span>` : ''}</dd></div>`;
                            }).join('')}
                        </dl>
                    </section>`).join('')}
               </div>
               <p class="ag-policy-meta">${fingerprint ? `Policy <span class="ag-mono">${escapeHtml(String(fingerprint).slice(0, 10))}</span> · ` : ''}Trade admission, sizing and exit rules are edited in the same Risk Manager dialog.</p>`
            : `<div class="ag-empty">
                    <h4>No active risk policy</h4>
                    <p>No Risk Constitution is active yet. Open the Risk Manager editor to choose your limits and confirm them.</p>
               </div>`;
        panel.className = 'ag-panel ag-policy';
        panel.innerHTML = `
            <div class="ag-panel-head">
                <div>
                    <h3>Risk Manager active policy</h3>
                    <p>${escapeHtml(status[1])}</p>
                </div>
                <div class="ag-head-actions">
                    ${chip(status[0], riskStatusTone(constitution.status))}
                    <button type="button" class="ds-btn ds-btn--sm" data-testid="agents-risk-policy-configure"
                        onclick="Actions.toggleRiskSettings()">Configure</button>
                </div>
            </div>
            ${body}`;
    }

    function opportunityStatusLabel(value) {
        const status = String(value || '').toLowerCase();
        if (status === 'candidate_found') return { label: 'Candidate found', cls: 'is-ready' };
        if (status === 'reviewed') return { label: 'Cycle completed', cls: 'is-ready' };
        if (status === 'no_eligible_candidates') return { label: 'No eligible setup', cls: 'is-waiting' };
        if (status === 'no_qualifying_v2_setup' || status === 'no_qualifying_cross_sectional_setup' || status === 'no_candidates') return { label: 'No setup now', cls: 'is-waiting' };
        if (status === 'stale_scanner_snapshot') return { label: 'Awaiting fresh scan', cls: 'is-warning' };
        if (status === 'error') return { label: 'Diagnostic error', cls: 'is-danger' };
        return { label: status ? status.replaceAll('_', ' ') : 'Awaiting cycle', cls: 'is-waiting' };
    }

    function opportunityTone(cls) {
        return { 'is-ready': 'pos', 'is-warning': 'warn', 'is-danger': 'neg' }[cls] || '';
    }

    function renderOpportunityMonitor(autonomous, bot, unavailableReason = '') {
        const panel = document.getElementById('agents-opportunity-monitor');
        if (!panel) return;
        if (unavailableReason) {
            // The status request failed: do not draw an "awaiting cycle" board of
            // zeros as if the engine had reported an empty cycle.
            panel.className = 'ag-panel ag-monitor';
            panel.innerHTML = `
                <div class="ag-panel-head">
                    <div>
                        <h3>Risk Manager signal monitor</h3>
                        <p>Price-first discovery across Scanner, Watchlist, and Pre-Earnings.</p>
                    </div>
                    ${chip('Status unavailable', 'neg')}
                </div>
                <div class="ag-note ag-note--neg" role="alert"><i class="fa-solid fa-triangle-exclamation" aria-hidden="true"></i>
                    <span>${escapeHtml(unavailableReason)}. No cycle data is shown; press Refresh to retry.</span></div>`;
            return;
        }
        const result = autonomous && typeof autonomous.result === 'object' ? autonomous.result : {};
        const cycle = result && typeof result.cycle === 'object' ? result.cycle : {};
        const allocation = cycle && typeof cycle.allocation === 'object' ? cycle.allocation : {};
        const targetBucket = String(allocation.target_bucket || '').replaceAll('_', ' ');
        const targetPct = Number(allocation.normalized_pct?.[allocation.target_bucket]);
        const sources = cycle && typeof cycle.sources === 'object' ? cycle.sources : {};
        const v2 = sources && typeof sources.v2_forward === 'object' ? sources.v2_forward : {};
        const scanner = sources && typeof sources.scanner === 'object' ? sources.scanner : {};
        const watchlist = sources && typeof sources.watchlist === 'object' ? sources.watchlist : {};
        const preEarnings = sources && typeof sources.pre_earnings === 'object' ? sources.pre_earnings : {};
        const research = sources && typeof sources.cross_sectional_momentum === 'object'
            ? sources.cross_sectional_momentum
            : {};
        const status = opportunityStatusLabel(cycle.status || v2.status);
        const researchRanked = Array.isArray(research.evaluations)
            ? research.evaluations.filter((item) => Number(item?.rank) > 0).sort((a, b) => Number(a.rank) - Number(b.rank))
            : [];
        const researchLeader = researchRanked[0] || null;
        const updatedAt = autonomous?.updated_at || autonomous?.requested_at;
        const updated = updatedAt ? relativeTime(updatedAt) : 'No completed cycle yet';
        const limits = cycle && typeof cycle.limits === 'object'
            ? cycle.limits
            : (state.executionControl?.autonomous_cycle_limits || {});
        const scannerObserved = Number(scanner.observation_count || 0);
        const watchlistObserved = Number(watchlist.observation_count || 0);
        const preEarningsEvaluated = Number(preEarnings.evaluated || 0);
        const totalObservations = scannerObserved + watchlistObserved + preEarningsEvaluated;
        const candidateLimit = Number(limits.candidate_reviews_per_cycle || 0);
        const tradeLimit = Number(limits.trade_submissions_per_cycle || 1);
        const scannerBucketLimit = Number(limits.scanner_symbols_per_bucket || 0);
        const watchlistLimit = Number(limits.watchlist_symbols || 0);
        const actualBuckets = Array.isArray(allocation.actual_buckets)
            ? allocation.actual_buckets.map((item) => String(item).replaceAll('_', ' ')).join(', ')
            : '';
        const cycleGate = resolveEntryGate(bot || {}, state.snapshot?.config || {}, state.executionControl);
        // Only the internal candidate worker reads a queued cycle request: with
        // it off the button answered "cycle triggered" and nothing ever ran.
        const workerGate = (Array.isArray(cycleGate.gates) ? cycleGate.gates : [])
            .find((gate) => gate && gate.key === 'candidate_worker') || null;
        const workerOff = !!workerGate && workerGate.ready !== true;
        const runDisabled = bot?.running !== true
            || bot?.engine_running === false
            || state.executionControl?.readiness?.ready !== true
            || workerOff;
        // The server refuses a manual cycle for the same reasons; say which one
        // instead of leaving a greyed-out button with no explanation.
        const runBlocker = cycleGate.blockers[0] || (workerOff ? workerGate : null);
        const runDisabledReason = !runDisabled
            ? 'Queue one gated evaluation cycle on the engine now'
            : (runBlocker
                ? `Unavailable — ${runBlocker.label}: ${runBlocker.detail || 'not ready'}`
                : 'Unavailable until paper-entry readiness is verified');
        // A cycle result only exists once the engine recorded one.
        const hasCycle = cycle && typeof cycle.sources === 'object' && cycle.sources !== null;
        const monitorInterval = Math.max(30, Number(state.snapshot?.config?.agents?.execution?.autonomous_monitor_interval_seconds) || 120);
        const cycleAgeSeconds = dateMs(updatedAt) === null ? null : (Date.now() - dateMs(updatedAt)) / 1000;
        const cycleOverdue = bot?.running === true && cycleAgeSeconds !== null
            && cycleAgeSeconds > Math.max(3 * monitorInterval, 600);
        const count = (value) => (hasCycle ? String(Number(value || 0)) : '—');
        const sourceState = (source) => (source.enabled === false
            ? { label: 'Disabled', tone: '' }
            : { label: 'Active', tone: 'pos' });
        const reportedState = (source) => {
            const raw = String(source.status || (source.enabled === false ? 'disabled' : 'awaiting'));
            const tone = raw === 'disabled' ? '' : opportunityTone(opportunityStatusLabel(source.status).cls);
            return { label: sentenceCase(raw), tone };
        };
        const lanes = [
            {
                label: 'Scanner',
                state: sourceState(scanner),
                observations: count(scannerObserved),
                candidates: count(scanner.candidate_count),
                note: 'Hot-market observations',
            },
            {
                label: 'Watchlist',
                state: sourceState(watchlist),
                observations: count(watchlistObserved),
                candidates: count(watchlist.candidate_count),
                note: hasCycle
                    ? `${Number(watchlist.stock_observation_count || 0)} stocks · ${Number(watchlist.crypto_observation_count || 0)} crypto`
                    : 'Stocks and crypto on the watchlist',
            },
            {
                label: 'Pre-Earnings',
                state: reportedState(preEarnings),
                observations: count(preEarningsEvaluated),
                candidates: count(preEarnings.candidate_count),
                note: 'Events evaluated · candidates executable now',
            },
        ];
        if (v2.enabled) {
            lanes.push({
                label: 'Forward V2',
                state: reportedState(v2),
                observations: count(v2.evaluated),
                candidates: count(v2.candidate_count),
                note: 'Setups evaluated',
            });
        }
        // enabled === false means the source is switched off, not "awaiting a cycle".
        const researchOff = hasCycle && research.enabled === false;
        const researchStatus = researchOff
            ? { label: 'Source disabled', cls: 'is-waiting' }
            : opportunityStatusLabel(research.status);
        const allocationWeights = state.executionControl?.autonomous_allocation?.buckets_pct || {};
        const weightSummary = ['stocks', 'crypto', 'pre_earnings']
            .map((bucket) => Number(allocationWeights[bucket]))
            .every((weight) => Number.isFinite(weight))
            ? `${['stocks', 'crypto', 'pre_earnings'].map((bucket) => Number(allocationWeights[bucket])).join('/')} weights (stocks/crypto/pre-earnings)`
            : 'Allocation weights unavailable';
        const researchEligible = Number(research.eligible_liquid_universe_size || research.universe_size || 0);
        const researchConfigured = Number(research.configured_universe_limit || 0);
        const researchVolume = Number(research.min_quote_volume_24h || 0);
        const researchUniverse = researchConfigured > 0
            ? `${researchEligible} / ${researchConfigured}`
            : `${researchEligible}`;
        const researchThreshold = researchVolume > 0
            ? `≥ $${(researchVolume / 1_000_000).toFixed(researchVolume >= 1_000_000 ? 0 : 2)}M 24h volume`
            : 'Liquidity threshold unavailable';
        const leaderChecks = Array.isArray(researchLeader?.failed_checks) && researchLeader.failed_checks.length
            ? researchLeader.failed_checks.join(' · ').replaceAll('_', ' ')
            : String(researchLeader?.reason || 'No ranked research observation');
        const researchDetail = researchLeader
            ? `${researchLeader.symbol}: rank ${researchLeader.rank}; ${leaderChecks}.`
            : 'No completed research observation is available yet.';
        lanes.push({
            label: 'Cross-sectional momentum',
            sub: 'Paper research only',
            state: { label: researchStatus.label, tone: opportunityTone(researchStatus.cls) },
            observations: researchOff || !hasCycle ? '—' : researchUniverse,
            candidates: researchOff ? '—' : count(research.candidate_count),
            note: researchOff
                ? 'This research source is switched off in the execution configuration, so the cycle collects no momentum observations for it.'
                : `Eligible symbols · ${researchThreshold} · ${researchDetail}`,
        });
        const rows = lanes.map((lane) => `
                        <tr>
                            <th scope="row">${escapeHtml(lane.label)}${lane.sub ? `<span class="ag-lane-sub">${escapeHtml(lane.sub)}</span>` : ''}</th>
                            <td>${chip(lane.state.label, lane.state.tone)}</td>
                            <td class="num">${escapeHtml(lane.observations)}</td>
                            <td class="num">${escapeHtml(lane.candidates)}</td>
                            <td class="ag-cell-detail">${escapeHtml(lane.note)}</td>
                        </tr>`).join('');

        panel.className = 'ag-panel ag-monitor';
        panel.innerHTML = `
            <div class="ag-panel-head">
                <div>
                    <h3>Risk Manager signal monitor</h3>
                    <p>Scanner, Watchlist, and Pre-Earnings share the gated AI → Critic → Risk → Capital → Paper Broker route. Research-only sources remain separately labeled.</p>
                </div>
                <div class="ag-head-actions">
                    ${chip(status.label, opportunityTone(status.cls))}
                    <button class="ds-btn" type="button" onclick="Actions.runRiskManagerCycle()"
                        ${runDisabled ? 'disabled' : ''} data-testid="agents-run-v2-cycle" title="${escapeHtml(runDisabledReason)}">
                        <i class="fa-solid fa-play" aria-hidden="true"></i> Run signal cycle
                    </button>
                </div>
            </div>
            ${runDisabled ? `<p class="ag-inline-note" data-testid="agents-run-cycle-blocked">Run signal cycle: ${escapeHtml(runDisabledReason)}.</p>` : ''}
            <div class="ds-stats ag-monitor-stats">
                <div class="ds-stat"><span class="ds-label">Source observations</span><strong>${hasCycle ? totalObservations : '—'}</strong><small>${hasCycle ? `Scanner ${scannerObserved} · Watchlist ${watchlistObserved} · Pre-Earnings ${preEarningsEvaluated}` : 'No cycle result recorded yet'}</small></div>
                <div class="ds-stat"><span class="ds-label">Candidates</span><strong>${count(cycle.candidate_count)}</strong><small>${hasCycle ? `${Number(cycle.reviewed || 0)} reviewed · ${Number(cycle.submitted || 0)} sent to the entry gates` : 'No cycle result recorded yet'}</small></div>
                <div class="ds-stat"><span class="ds-label">Scan cap</span><strong>${scannerBucketLimit || 'N/A'} / bucket</strong><small>Stocks + Crypto · Watchlist ${watchlistLimit || 'N/A'}</small></div>
                <div class="ds-stat"><span class="ds-label">Cycle gate</span><strong>${candidateLimit > 0 ? candidateLimit : 'Unlimited'} reviews</strong><small>Max ${tradeLimit} submission(s) / cycle</small></div>
                <div class="ds-stat"><span class="ds-label">Last completed</span><strong>${escapeHtml(updated)}</strong><small>${escapeHtml(sentenceCase(autonomous?.trigger || 'scheduled'))}${cycleOverdue ? ` · overdue (expected every ${Math.round(monitorInterval)}s)` : ''}</small></div>
                <div class="ds-stat"><span class="ds-label">Allocation lane</span><strong>${escapeHtml(sentenceCase(targetBucket) || (hasCycle ? 'Not selected' : 'Awaiting cycle'))}</strong><small>${Number.isFinite(targetPct) ? `${targetPct.toFixed(2)}% target` : escapeHtml(weightSummary)}${actualBuckets ? ` · used ${escapeHtml(actualBuckets)}` : ''}</small></div>
            </div>
            <div class="ag-table-wrap">
                <table class="ag-table ag-lanes" aria-label="Signal sources in the last cycle">
                    <thead>
                        <tr><th scope="col">Source</th><th scope="col">State</th><th scope="col" class="num">Observations</th><th scope="col" class="num">Candidates</th><th scope="col">Note</th></tr>
                    </thead>
                    <tbody>${rows}
                    </tbody>
                </table>
            </div>`;
    }

    function buildCards(config, runtimeStatus, bot) {
        return Object.entries(AGENTS).map(([key, meta]) => {
            const agentConfig = config.agents?.[key] || {};
            const enabled = asEnabled(agentConfig);
            const runtime = runtimeStatus[key] || {};
            return {
                key,
                meta,
                agentConfig,
                enabled,
                runtime,
                status: computeAgentState(key, enabled, runtime, bot),
                detail: configDetail(key, config, agentConfig, runtime),
            };
        });
    }

    const CATEGORY_ORDER = ['Trading', 'Safety', 'Discovery', 'Intelligence', 'Data', 'Research', 'Development', 'Maintenance'];

    function renderCards(cards, bot) {
        const grid = document.getElementById('agents-grid');
        const summary = document.getElementById('agents-summary');
        if (!grid || !summary) return;

        document.getElementById('risk-settings-modal')?.remove();

        const enabledCount = cards.filter((card) => card.enabled).length;
        const runningCount = cards.filter((card) => card.status.group === 'running').length;
        const attentionCount = cards.filter((card) => card.status.group === 'attention').length;
        summary.textContent = `${enabledCount} of ${cards.length} enabled · ${runningCount} running · ${attentionCount} need attention`;
        const filterCounts = {
            all: cards.length,
            running: runningCount,
            attention: attentionCount,
            disabled: cards.filter((card) => card.status.group === 'disabled').length,
        };
        document.querySelectorAll('[data-agents-count]').forEach((node) => {
            node.textContent = String(filterCounts[node.dataset.agentsCount] ?? '');
        });
        const rowHtml = (card) => {
            const { key, meta, enabled, runtime, status, detail } = card;
            const error = String(runtime.last_error || '').trim();
            const benignError = BENIGN_PAUSES.has(error.toLowerCase());
            const toggleLocked = meta.lockedWhenRunning && bot.running;
            // Real evidence first: the most recent run / success / engine heartbeat
            // the worker itself reported. "Engine managed" said nothing.
            const activityAt = runtime.last_activity_at || runtime.last_run || runtime.last_success || runtime.last_heartbeat;
            const activityKind = meta.engineBound && !runtime.last_run && !runtime.last_success ? 'Heartbeat ' : '';
            const runtimeLabel = activityAt
                ? `${activityKind}${relativeTime(activityAt)}`
                : (runtime.initializing === true
                    ? 'Starting…'
                    : (runtime.running === true ? 'No run reported yet' : 'No run recorded'));
            // Last measured run; the average only when no single run was timed.
            const duration = durationLabel(runtime.last_run_duration_ms ?? runtime.avg_duration_ms);
            const searchable = `${meta.title} ${meta.category} ${meta.description} ${status.label}`.toLowerCase();
            const riskEditorOpen = key === 'risk' && state.riskEditor.open;
            const healthReason = String(runtime.health_reason || '').trim();
            const blockReason = meta.masterBound && bot.running === true && runtime.accepting_new_entries === false
                ? String(runtime.entry_block_reason || '').trim()
                : '';
            const settings = riskEditorOpen
                ? ''
                : `
                        <div><dt>${escapeHtml(detail.left)}</dt><dd title="${escapeHtml(detail.leftValue)}">${escapeHtml(detail.leftValue)}</dd></div>
                        <div><dt>${escapeHtml(detail.right)}</dt><dd title="${escapeHtml(detail.rightValue)}">${escapeHtml(detail.rightValue)}</dd></div>`;
            // Outcome of the latest cycle, as the worker recorded it.
            const runFailed = runtime.last_run_ok === false;
            const runError = String(runtime.last_run_error || '').trim();
            const runFailedAt = runtime.last_run_error_at || runtime.last_run_finished_at || '';
            const runFailures = Number(runtime.consecutive_failures || 0);
            const runFailedNote = runFailed
                ? `Last run failed${runFailedAt ? ` ${relativeTime(runFailedAt).toLowerCase()}` : ''}${runError ? `: ${runError}` : ''}${runFailures > 1 ? ` (${runFailures} runs in a row)` : ''}`
                : '';
            const errorRepeatsRun = runFailed && (!runError || runError.toLowerCase().includes(error.toLowerCase()) || error.toLowerCase().includes(runError.toLowerCase()));
            const notes = [
                runFailedNote ? `<p class="ag-row-note ag-row-note--neg" data-testid="agent-last-run-failed" title="${escapeHtml(String(runFailedAt))}">${escapeHtml(runFailedNote)}</p>` : '',
                error && !benignError && !errorRepeatsRun ? `<p class="ag-row-note ag-row-note--neg">Last error: ${escapeHtml(error)}</p>` : '',
                !error && enabled && status.group === 'attention' && healthReason ? `<p class="ag-row-note ag-row-note--warn">${escapeHtml(healthReason)}</p>` : '',
                blockReason ? `<p class="ag-row-note ag-row-note--warn">New entries blocked by ${escapeHtml(blockReason)} — see the master control above.</p>` : '',
            ].join('');
            return `
                <article class="agent-card ag-row" data-agent-key="${key}" data-state="${status.key}"
                    data-filter-group="${status.group}" data-search="${escapeHtml(searchable)}">
                    <div class="ag-row-id">
                        <h5 class="ag-row-title">${escapeHtml(meta.title)}</h5>
                        <p class="ag-row-desc">${escapeHtml(meta.description)}</p>
                    </div>
                    <div class="ag-row-state">
                        <span class="agents-status-pill agents-status-${status.key}">${escapeHtml(status.label)}</span>
                        ${notes}
                    </div>
                    <dl class="ag-row-settings ${riskEditorOpen ? 'is-risk-editing' : ''}">${settings}</dl>
                    <div class="ag-row-run">
                        <span class="ag-row-label">Last activity</span>
                        <span class="ag-row-num" title="${escapeHtml(activityAt ? String(activityAt) : runtimeLabel)}">${escapeHtml(runtimeLabel)}</span>
                    </div>
                    <div class="ag-row-run">
                        <span class="ag-row-label">Last run took</span>
                        <span class="ag-row-num">${escapeHtml(duration)}</span>
                    </div>
                    <div class="ag-row-controls">
                        ${key === 'ui_inspector' ? `
                            <button class="ds-btn ds-btn--sm ds-btn--ghost ag-row-btn" type="button"
                                title="Run the tab and data health inspection now" aria-label="Run Tab & Data Health Inspection Now"
                                data-testid="ui-inspector-run" onclick="Actions.runUiInspector(this)">
                                <i class="fa-solid fa-play" aria-hidden="true"></i> Run now
                            </button>` : ''}
                        ${key === 'research_factory' ? `
                            <button class="ds-btn ds-btn--sm ds-btn--ghost ag-row-btn" type="button"
                                title="Run one research cycle now" aria-label="Run Research Cycle Now"
                                data-testid="research-factory-run" onclick="Actions.runResearchCycle(this)">
                                <i class="fa-solid fa-play" aria-hidden="true"></i> Run now
                            </button>` : ''}
                        ${key === 'risk' ? `
                            <button class="ds-btn ds-btn--sm ds-btn--ghost ag-row-btn ${riskEditorOpen ? 'is-active' : ''}" type="button"
                                title="Configure Risk Manager limits" aria-label="Configure Risk Manager limits"
                                aria-expanded="${riskEditorOpen ? 'true' : 'false'}" aria-controls="risk-settings-form"
                                data-testid="risk-settings-open" onclick="Actions.toggleRiskSettings()">
                                Configure
                            </button>` : ''}
                        <label class="agents-toggle ag-toggle" title="${toggleLocked ? 'Stop the bot before disabling this safety-critical agent.' : `Enable or disable ${meta.title}`}">
                            <input type="checkbox" aria-label="Enable ${escapeHtml(meta.title)}" data-testid="agent-toggle-${key}"
                                ${enabled ? 'checked' : ''} ${toggleLocked ? 'disabled' : ''}
                                onchange="Actions.toggleAgent('${key}', this.checked, this)">
                            <span class="ag-toggle-track" aria-hidden="true"></span>
                        </label>
                    </div>
                </article>`;
        };
        const groups = [];
        cards.forEach((card) => {
            const name = String(card.meta.category || 'Other');
            let group = groups.find((item) => item.name === name);
            if (!group) {
                group = { name, cards: [] };
                groups.push(group);
            }
            group.cards.push(card);
        });
        const rank = (name) => (CATEGORY_ORDER.includes(name) ? CATEGORY_ORDER.indexOf(name) : CATEGORY_ORDER.length);
        groups.sort((a, b) => rank(a.name) - rank(b.name));
        grid.innerHTML = `
            <div class="ag-fleet-cols" aria-hidden="true">
                <span>Agent</span><span>State</span><span>Key settings</span><span>Last activity</span><span>Last run took</span><span>Enabled</span>
            </div>
            ${groups.map((group) => `
            <section class="ag-group" aria-label="${escapeHtml(group.name)} agents">
                <h4 class="ag-group-head">${escapeHtml(group.name)}</h4>
                ${group.cards.map(rowHtml).join('')}
            </section>`).join('')}`;
        if (state.riskEditor.open) {
            grid.insertAdjacentHTML(
                'afterend',
                riskEditorHtml(state.snapshot?.config?.risk_constitution || {})
            );
            document.body.classList.add('agents-risk-modal-open');
            syncRiskSettingsForm(document.getElementById('risk-settings-form'));
        } else {
            document.body.classList.remove('agents-risk-modal-open');
        }
        applyFilters();
    }

    function applyFilters() {
        const cards = Array.from(document.querySelectorAll('#agents-grid .agent-card'));
        const query = state.search.trim().toLowerCase();
        let visible = 0;
        cards.forEach((card) => {
            const filterMatch = state.filter === 'all' || card.dataset.filterGroup === state.filter;
            const searchMatch = !query || String(card.dataset.search || '').includes(query);
            const show = filterMatch && searchMatch;
            card.classList.toggle('hidden', !show);
            if (show) visible += 1;
        });
        document.getElementById('agents-empty')?.classList.toggle('hidden', visible !== 0);
    }

    function validateRiskSettingsForm(form, requireComplete = false) {
        const constitution = state.snapshot?.config?.risk_constitution || {};
        const schema = constitution.schema;
        const tradeSchema = constitution.trade_policy?.schema;
        if (!Array.isArray(schema) || !schema.length || !Array.isArray(tradeSchema) || !tradeSchema.length) {
            return { values: {}, tradeValues: {}, complete: false, missingCount: 0, error: 'Risk Manager schema is unavailable.', firstInvalid: null };
        }
        const values = {};
        const tradeValues = {};
        const missing = [];
        let error = '';
        let firstInvalid = null;

        const readFields = (fields, target) => {
            fields.forEach((field) => {
                const input = form?.elements?.[field.name];
                const raw = String(input?.value ?? '').trim();
                let value = null;
                let fieldError = '';
                if (!raw) {
                    missing.push(field.label);
                } else if (field.kind === 'boolean') {
                    if (raw === 'true') value = true;
                    else if (raw === 'false') value = false;
                    else fieldError = `${field.label} must be Yes or No.`;
                } else if (field.kind === 'enum') {
                    const options = Array.isArray(field.options) ? field.options.map(String) : [];
                    if (options.includes(raw)) value = raw;
                    else fieldError = `${field.label} has an invalid option.`;
                } else {
                    value = Number(raw);
                    if (!Number.isFinite(value)) {
                        fieldError = `${field.label} must be a number.`;
                    } else if (field.kind === 'integer' && !Number.isInteger(value)) {
                        fieldError = `${field.label} must be a whole number.`;
                    } else if (field.minimum !== null && field.minimum !== undefined && value < Number(field.minimum)) {
                        fieldError = `${field.label} must be at least ${field.minimum}.`;
                    } else if (field.maximum !== null && field.maximum !== undefined && value > Number(field.maximum)) {
                        fieldError = `${field.label} must be at most ${field.maximum}.`;
                    }
                }
                target[field.name] = value;
                input?.setAttribute('aria-invalid', fieldError ? 'true' : 'false');
                if (fieldError && !error) {
                    error = fieldError;
                    firstInvalid = input;
                }
            });
        };
        readFields(schema, values);
        readFields(tradeSchema, tradeValues);

        if (!error && values.max_risk_per_trade_pct !== null && values.max_daily_loss_pct !== null
            && values.max_risk_per_trade_pct > values.max_daily_loss_pct) {
            error = 'Max risk / trade cannot exceed max daily loss.';
            firstInvalid = form?.elements?.max_risk_per_trade_pct || null;
            firstInvalid?.setAttribute('aria-invalid', 'true');
        }
        if (!error && values.max_position_size_pct !== null && values.max_total_exposure_pct !== null
            && values.max_position_size_pct > values.max_total_exposure_pct) {
            error = 'Max position size cannot exceed max total exposure.';
            firstInvalid = form?.elements?.max_position_size_pct || null;
            firstInvalid?.setAttribute('aria-invalid', 'true');
        }
        if (!error && tradeValues.unprofitable_position_cap_pct !== null
            && tradeValues.base_position_capital_pct !== null
            && tradeValues.unprofitable_position_cap_pct > tradeValues.base_position_capital_pct) {
            error = 'Unprofitable-strategy cap cannot exceed base capital / position.';
            firstInvalid = form?.elements?.unprofitable_position_cap_pct || null;
            firstInvalid?.setAttribute('aria-invalid', 'true');
        }
        if (!error && tradeValues.base_position_capital_pct !== null
            && values.max_position_size_pct !== null
            && tradeValues.base_position_capital_pct > values.max_position_size_pct) {
            error = 'Base capital / position cannot exceed the Risk Constitution max position size.';
            firstInvalid = form?.elements?.base_position_capital_pct || null;
            firstInvalid?.setAttribute('aria-invalid', 'true');
        }
        if (!error && requireComplete && missing.length) {
            error = `Complete every field before activation (${missing.length} remaining).`;
            firstInvalid = [...schema, ...tradeSchema]
                .map((field) => form?.elements?.[field.name])
                .find((input) => !String(input?.value ?? '').trim()) || null;
            firstInvalid?.setAttribute('aria-invalid', 'true');
        }
        return { values, tradeValues, complete: missing.length === 0 && !error, missingCount: missing.length, error, firstInvalid };
    }

    function syncRiskSettingsForm(form, requireComplete = false) {
        const validation = validateRiskSettingsForm(form, requireComplete);
        state.riskEditor.values = { ...validation.values };
        state.riskEditor.tradeValues = { ...validation.tradeValues };
        state.riskEditor.error = validation.error;
        const errorNode = form?.querySelector('[data-testid="risk-settings-error"]');
        if (errorNode) {
            errorNode.textContent = validation.error;
            errorNode.classList.toggle('hidden', !validation.error);
        }
        const draftSave = form?.querySelector('[data-testid="risk-settings-save-draft"]');
        if (draftSave) draftSave.disabled = Boolean(validation.error) || state.riskEditor.saving;
        const activate = form?.querySelector('[data-testid="risk-settings-save"]');
        if (activate) activate.disabled = Boolean(validation.error) || state.riskEditor.saving;
        const completion = form?.querySelector('[data-testid="risk-settings-completion"]');
        if (completion) {
            completion.textContent = validation.complete
                ? 'All settings are complete.'
                : `${validation.missingCount} settings remaining.`;
        }
        return validation;
    }

    function renderCurrentAgentCards() {
        if (state.snapshot?.cards && state.snapshot?.bot) {
            renderCards(state.snapshot.cards, state.snapshot.bot);
        }
    }

    async function responseJson(response, fallback) {
        if (!response || !response.ok) {
            let message = fallback;
            try {
                const payload = await response.json();
                const detail = payload.detail || payload.message;
                message = Array.isArray(detail)
                    ? detail.map((item) => item?.msg || String(item)).join('; ')
                    : (typeof detail === 'string' ? detail : fallback);
            } catch (_) { }
            throw new Error(message);
        }
        return response.json();
    }

    async function waitForAgentRuntime(agentName, shouldRun, timeoutMs = 15000) {
        const deadline = Date.now() + timeoutMs;
        while (Date.now() < deadline) {
            await new Promise((resolve) => window.setTimeout(resolve, 600));
            await UI.renderAgents({ force: true });
            const card = state.snapshot?.cards?.find((item) => item.key === agentName);
            if (!card) continue;
            if (card.runtime?.lifecycle_state === 'error') {
                throw new Error(card.runtime.last_error || `${AGENTS[agentName].title} failed to start.`);
            }
            if (shouldRun && card.runtime?.running === true) return card.runtime;
            if (!shouldRun && card.runtime?.running !== true) return card.runtime;
        }
        throw new Error(`${AGENTS[agentName].title} did not confirm ${shouldRun ? 'startup' : 'shutdown'} in time.`);
    }

    UI.renderAgents = async function (options = {}) {
        if (state.loading) return;
        state.loading = true;
        const controlPlane = document.getElementById('agents-control-plane');
        const refresh = document.getElementById('agents-refresh-btn');
        controlPlane?.setAttribute('aria-busy', 'true');
        if (refresh) refresh.disabled = true;

        try {
            const [controlResponse, autonomousResponse, executionResponse] = await Promise.all([
                API.fetchWithTimeout('/api/agents/control-plane', {
                    timeout: 10000,
                    bypassCooldown: true,
                }),
                API.fetchWithTimeout('/api/agents/risk/pipeline-status', {
                    timeout: 10000,
                    bypassCooldown: true,
                }),
                API.fetchWithTimeout('/api/execution/control', {
                    timeout: 10000,
                    bypassCooldown: true,
                }),
            ]);
            const payload = await responseJson(controlResponse, 'Could not load the automation control plane.');
            const executionControl = await responseJson(executionResponse, 'Could not load Risk Manager execution status.');
            let autonomous = null;
            let autonomousError = '';
            try {
                autonomous = await responseJson(autonomousResponse, 'Could not load the V2 opportunity monitor.');
            } catch (error) {
                console.warn('V2 opportunity monitor unavailable:', error);
                autonomousError = error?.message || 'Signal monitor status unavailable';
            }
            const config = payload?.config || {};
            const bot = payload?.bot || {};
            const runtimeStatus = payload?.agents && typeof payload.agents === 'object'
                ? payload.agents
                : {};
            const entryGate = payload?.entry_gate && Array.isArray(payload.entry_gate.gates)
                ? payload.entry_gate
                : null;
            state.executionControl = executionControl;
            // Set before buildCards: every renderer reads the same entry gate.
            state.snapshot = { config, runtimeStatus, bot, cards: [], executionControl, entryGate };
            const cards = buildCards(config, runtimeStatus, bot);
            state.snapshot.cards = cards;
            renderMaster(bot, config);
            renderKpis(bot, cards);
            renderReadiness(bot, config, executionControl);
            renderRiskPolicy(config);
            renderOpportunityMonitor(autonomous, bot, autonomousError);
            renderCards(cards, bot);

            const updated = document.getElementById('agents-last-updated');
            if (updated) updated.textContent = `Updated ${new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}`;
        } catch (error) {
            if (API.isNetworkFailure(error)) API._warnNetworkOnce('Failed to load Agents', error);
            else console.error('Failed to load Agents:', error);
            const grid = document.getElementById('agents-grid');
            if (grid) {
                grid.innerHTML = `<div class="ag-note ag-note--neg" role="alert"><i class="fa-solid fa-triangle-exclamation" aria-hidden="true"></i><span>${escapeHtml(error.message || 'Agents unavailable')}. Press Refresh to retry.</span></div>`;
            }
            UI.showToast(error.message || 'Failed to load Agents', 'error');
        } finally {
            state.loading = false;
            controlPlane?.setAttribute('aria-busy', 'false');
            if (refresh) refresh.disabled = false;
        }
    };

    UI.setAgentsFilter = function (filter, button) {
        state.filter = ['all', 'running', 'attention', 'disabled'].includes(filter) ? filter : 'all';
        document.querySelectorAll('.agents-filter').forEach((node) => node.classList.toggle('active', node === button));
        applyFilters();
    };

    UI.filterAgents = function (value) {
        state.search = String(value || '');
        applyFilters();
    };

    Actions.setBotRunning = async function (enabled) {
        const snapshot = state.snapshot;
        if (enabled && snapshot?.bot?.engine_running === false) {
            UI.showToast('Start the market engine before starting the bot.', 'warning');
            return;
        }
        // Starting hands paper entries to the automation; stopping is a safety
        // action and stays one click.
        if (enabled && typeof window.confirm === 'function'
            && !window.confirm('Start the paper bot? Signals that pass every risk gate can then open paper positions automatically.')) {
            return;
        }
        document.querySelectorAll('[data-testid="agents-bot-start"], [data-testid="agents-bot-stop"]')
            .forEach((button) => { button.disabled = true; });
        const liveRegion = document.getElementById('agents-live-region');
        if (liveRegion) liveRegion.textContent = `${enabled ? 'Starting' : 'Stopping'} Risk-managed bot…`;

        try {
            const response = await API.fetchWithTimeout(`/api/bot/${enabled ? 'start' : 'stop'}`, {
                method: 'POST',
                timeout: 20000,
                bypassCooldown: true,
            });
            const result = await responseJson(response, `Could not ${enabled ? 'start' : 'stop'} the bot.`);
            if (['error', 'failed', 'blocked'].includes(result.status)) {
                throw new Error(result.message || `Bot ${enabled ? 'start' : 'stop'} failed.`);
            }
            UI.showToast(`Risk-managed bot ${enabled ? 'started' : 'stopped'}.`, 'success');
            if (liveRegion) liveRegion.textContent = `Risk-managed bot ${enabled ? 'started' : 'stopped'}.`;
        } catch (error) {
            console.error('Bot control failed:', error);
            UI.showToast(error.message || 'Bot control failed.', 'error');
            if (liveRegion) liveRegion.textContent = error.message || 'Bot control failed.';
        } finally {
            await UI.renderAgents({ force: true });
        }
    };

    Actions.setPaperKillSwitch = async function (active) {
        // Releasing the stop re-opens automated paper risk: the Cockpit asks
        // twice for it, so one stray click here must not do it silently.
        if (active !== true && typeof window.confirm === 'function'
            && !window.confirm('Release the new-entry stop? Automated paper entries can resume immediately.')) {
            return;
        }
        try {
            const response = await API.fetchWithTimeout('/api/execution/kill-switch', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ active: active === true }),
                timeout: 10000,
                bypassCooldown: true,
            });
            await responseJson(response, 'Could not update the Risk Manager kill switch.');
            UI.showToast(
                active ? 'New automated risk stopped. Monitoring remains active.' : 'New-risk stop released.',
                active ? 'warning' : 'success'
            );
        } catch (error) {
            UI.showToast(error.message || 'Could not update the Risk Manager kill switch.', 'error');
        } finally {
            await UI.renderAgents({ force: true });
        }
    };

    Actions.reconcilePaperExecution = async function () {
        try {
            const response = await API.fetchWithTimeout('/api/execution/reconcile', {
                method: 'POST',
                timeout: 15000,
                bypassCooldown: true,
            });
            const result = await responseJson(response, 'Paper execution reconciliation failed.');
            const healthy = result?.status === 'HEALTHY';
            UI.showToast(
                healthy ? 'Paper execution reconciliation is healthy.' : 'Execution state conflict detected.',
                healthy ? 'success' : 'error'
            );
        } catch (error) {
            UI.showToast(error.message || 'Paper execution reconciliation failed.', 'error');
        } finally {
            await UI.renderAgents({ force: true });
        }
    };

    Actions.toggleAgent = async function (agentName, enabled, input) {
        if (!Object.prototype.hasOwnProperty.call(AGENTS, agentName)) return;
        if (input) input.disabled = true;
        const card = input?.closest('.agent-card');
        const pill = card?.querySelector('.agents-status-pill');
        if (pill) {
            pill.className = 'agents-status-pill agents-status-starting';
            pill.textContent = enabled ? 'Starting…' : 'Stopping…';
        }
        try {
            const response = await API.fetchWithTimeout(`/api/agents/${encodeURIComponent(agentName)}/enabled`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ enabled: enabled === true }),
                timeout: 15000,
                bypassCooldown: true,
            });
            const result = await responseJson(response, `Could not update ${AGENTS[agentName].title}.`);
            const runtime = result?.runtime || {};
            if (enabled && runtime.running !== true) {
                if (runtime.initializing === true || runtime.lifecycle_state === 'starting') {
                    await waitForAgentRuntime(agentName, true);
                } else {
                    throw new Error(`${AGENTS[agentName].title} was enabled but its worker did not start.`);
                }
            } else if (!enabled && runtime.running === true) {
                await waitForAgentRuntime(agentName, false);
            }
            if (AGENTS[agentName].engineBound) {
                // The API only saved the switch; the engine subprocess starts or
                // stops its own worker when its config watcher sees the change
                // (about 1s). Do not claim a worker state the API cannot observe.
                UI.showToast(`${AGENTS[agentName].title} ${enabled ? 'enabled' : 'disabled'}. The engine applies the switch within a few seconds; the card shows its next heartbeat.`, 'success');
            } else {
                UI.showToast(`${AGENTS[agentName].title} ${enabled ? 'started' : 'stopped'}.`, 'success');
            }
        } catch (error) {
            console.error('Agent update failed:', error);
            UI.showToast(error.message || 'Failed to update agent.', 'error');
        } finally {
            await UI.renderAgents({ force: true });
        }
    };

    Actions.runUiInspector = async function (btn) {
        const icon = btn?.querySelector('i');
        const origClass = icon?.className || 'fa-solid fa-play';
        if (btn) btn.disabled = true;
        if (icon) icon.className = 'fa-solid fa-spinner fa-spin';
        try {
            UI.showToast('Running tab-by-tab UI inspection…', 'info');
            const response = await API.fetchWithTimeout('/api/ui-inspector/run', {
                method: 'POST',
                timeout: 120000,
                bypassCooldown: true,
            });
            // A failed run used to fall through to "all 0 tabs healthy".
            const data = await responseJson(response, 'UI Inspector run failed.');
            const tabsChecked = Number(data?.tabs_checked);
            const anom = Number(data?.anomalies_detected);
            if (!Number.isFinite(tabsChecked) || !Number.isFinite(anom) || tabsChecked <= 0) {
                throw new Error('UI Inspector returned no inspection report.');
            }
            const tabHealth = data?.tab_health && typeof data.tab_health === 'object' ? data.tab_health : {};
            const notHealthy = Object.entries(tabHealth)
                .filter(([, item]) => item && item.status && item.status !== 'HEALTHY')
                .map(([name, item]) => `${name.replaceAll('_', ' ')} ${String(item.status).toLowerCase()}`);
            if (anom === 0 && notHealthy.length === 0) {
                UI.showToast(`Inspection complete: all ${tabsChecked} tabs healthy.`, 'success');
            } else {
                const escalated = Number(data?.escalations_sent) || 0;
                const healed = Number(data?.self_healed_count) || 0;
                UI.showToast(
                    `Inspection complete: ${anom} anomal${anom === 1 ? 'y' : 'ies'} across ${tabsChecked} tabs`
                    + ` · ${escalated} escalated to Coding Agent · ${healed} self-healed`
                    + (notHealthy.length ? ` · ${notHealthy.slice(0, 4).join(', ')}` : ''),
                    'warning'
                );
            }
        } catch (error) {
            console.error('UI Inspector run failed:', error);
            UI.showToast(error.message || 'UI Inspector run failed.', 'error');
        } finally {
            if (btn) btn.disabled = false;
            if (icon) icon.className = origClass;
            await UI.renderAgents({ force: true });
        }
    };

    Actions.runResearchCycle = async function (btn) {
        const icon = btn?.querySelector('i');
        const origClass = icon?.className || 'fa-solid fa-play';
        if (btn) btn.disabled = true;
        if (icon) icon.className = 'fa-solid fa-spinner fa-spin';
        try {
            UI.showToast('Running one bounded Research Factory cycle…', 'info');
            const response = await API.fetchWithTimeout('/api/research/auto-cycle/run', {
                method: 'POST',
                timeout: 120000,
                bypassCooldown: true,
            });
            const data = await responseJson(response, 'Research Factory cycle failed.');
            const result = data?.result || {};
            UI.showToast(
                `Research cycle complete: ${result.weaknesses_detected || 0} weaknesses · ${result.experiments || 0} experiments.`,
                'success'
            );
        } catch (error) {
            console.error('Research cycle failed:', error);
            UI.showToast(error.message || 'Research cycle failed.', 'error');
        } finally {
            if (btn) btn.disabled = false;
            if (icon) icon.className = origClass;
            await UI.renderAgents({ force: true });
        }
    };

    Actions.toggleRiskSettings = function () {
        if (state.riskEditor.saving) return;
        if (state.riskEditor.open) {
            Actions.closeRiskSettings();
            return;
        }
        const constitution = state.snapshot?.config?.risk_constitution || {};
        const schema = Array.isArray(constitution.schema) ? constitution.schema : [];
        const draft = constitution.draft || {};
        const tradeSchema = Array.isArray(constitution.trade_policy?.schema) ? constitution.trade_policy.schema : [];
        const tradeDraft = constitution.trade_policy?.draft || {};
        const values = Object.fromEntries(
            schema.map((field) => [field.name, draft[field.name] ?? null])
        );
        const tradeValues = Object.fromEntries(
            tradeSchema.map((field) => [field.name, tradeDraft[field.name] ?? null])
        );
        const matchedProfile = riskProfileItems(constitution)
            .find((profile) => riskValuesEqual(values, profile.values, schema));
        state.riskEditor = {
            open: true,
            saving: false,
            values,
            customValues: { ...values },
            tradeValues,
            profileId: matchedProfile?.id || 'custom',
            error: '',
        };
        renderCurrentAgentCards();
        window.setTimeout(() => document.querySelector('#risk-settings-form [data-risk-field]')?.focus(), 0);
    };

    Actions.closeRiskSettings = function () {
        if (state.riskEditor.saving) return;
        state.riskEditor = {
            open: false,
            saving: false,
            values: {},
            customValues: {},
            tradeValues: {},
            profileId: 'custom',
            error: '',
        };
        document.body.classList.remove('agents-risk-modal-open');
        renderCurrentAgentCards();
        window.setTimeout(() => document.querySelector('[data-testid="risk-settings-open"]')?.focus(), 0);
    };

    Actions.handleRiskSettingsBackdrop = function (event) {
        if (event?.target?.id === 'risk-settings-modal') Actions.closeRiskSettings();
    };

    Actions.selectRiskConstitutionProfile = function (profileId) {
        if (state.riskEditor.saving) return;
        const constitution = state.snapshot?.config?.risk_constitution || {};
        const schema = Array.isArray(constitution.schema) ? constitution.schema : [];
        const normalizedId = String(profileId || 'custom');
        let values;
        if (normalizedId === 'custom') {
            values = Object.fromEntries(
                schema.map((field) => [field.name, state.riskEditor.customValues?.[field.name] ?? null])
            );
        } else {
            const profile = riskProfileItems(constitution).find((item) => item.id === normalizedId);
            if (!profile) {
                UI.showToast('That Risk Constitution profile is unavailable.', 'error');
                return;
            }
            values = Object.fromEntries(
                schema.map((field) => [field.name, profile.values?.[field.name] ?? null])
            );
        }
        state.riskEditor.profileId = normalizedId;
        state.riskEditor.values = values;
        state.riskEditor.error = '';
        renderCurrentAgentCards();
        window.setTimeout(() => document.querySelector('#risk-settings-form [data-risk-field]')?.focus(), 0);
    };

    Actions.updateRiskSettingsDraft = function (form) {
        const validation = syncRiskSettingsForm(form);
        state.riskEditor.values = { ...validation.values };
        state.riskEditor.customValues = { ...validation.values };
        state.riskEditor.tradeValues = { ...validation.tradeValues };
        if (state.riskEditor.profileId !== 'custom') {
            state.riskEditor.profileId = 'custom';
            form?.querySelectorAll('[data-risk-profile-id]').forEach((button) => {
                const selected = button.dataset.riskProfileId === 'custom';
                button.classList.toggle('is-selected', selected);
                button.setAttribute('aria-pressed', selected ? 'true' : 'false');
                const action = button.querySelector('.agents-risk-profile-head span');
                if (action) action.textContent = selected ? 'Selected' : (
                    button.dataset.riskProfileId === 'custom' ? 'Edit freely' : 'Use profile'
                );
            });
        }
    };

    Actions.saveRiskSettings = async function (event, form, activate = true) {
        event?.preventDefault();
        if (state.riskEditor.saving) return;
        const validation = syncRiskSettingsForm(form, Boolean(activate));
        if (validation.error) {
            validation.firstInvalid?.focus();
            return;
        }

        const save = activate
            ? form.querySelector('[data-testid="risk-settings-save"]')
            : form.querySelector('[data-testid="risk-settings-save-draft"]');
        const buttons = Array.from(form.querySelectorAll('button'));
        state.riskEditor.saving = true;
        state.riskEditor.error = '';
        if (save) {
            save.disabled = true;
            save.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Saving...';
        }
        buttons.forEach((button) => { button.disabled = true; });

        try {
            const response = await API.fetchWithTimeout('/api/agents/risk/constitution', {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    owner_confirmed: Boolean(activate),
                    ...validation.values,
                    trade_policy: validation.tradeValues,
                }),
                timeout: 15000,
                bypassCooldown: true,
            });
            const result = await responseJson(response, 'Could not update the Risk Constitution.');
            state.riskEditor = {
                ...state.riskEditor,
                open: !activate,
                saving: false,
                values: { ...validation.values },
                customValues: { ...validation.values },
                tradeValues: { ...validation.tradeValues },
                error: '',
            };
            UI.showToast(
                activate ? 'Risk Constitution confirmed and applied live.' : 'Risk Constitution draft saved; active limits are unchanged.',
                'success'
            );
            const liveRegion = document.getElementById('agents-live-region');
            if (liveRegion) liveRegion.textContent = activate
                ? 'Owner-confirmed Risk Constitution applied to new paper entries.'
                : 'Risk Constitution draft saved without changing the active policy.';
            await UI.renderAgents({ force: true });
            if (!activate) {
                const refreshed = result?.constitution?.draft || validation.values;
                const refreshedTrade = result?.constitution?.trade_policy?.draft || validation.tradeValues;
                state.riskEditor.values = { ...refreshed };
                state.riskEditor.customValues = { ...refreshed };
                state.riskEditor.tradeValues = { ...refreshedTrade };
                syncRiskSettingsForm(document.getElementById('risk-settings-form'));
            }
        } catch (error) {
            console.error('Risk settings update failed:', error);
            state.riskEditor.saving = false;
            state.riskEditor.error = error.message || 'Could not update the Risk Constitution.';
            const errorNode = form.querySelector('[data-testid="risk-settings-error"]');
            if (errorNode) {
                errorNode.textContent = state.riskEditor.error;
                errorNode.classList.remove('hidden');
            }
            if (save) {
                save.disabled = false;
                save.innerHTML = activate
                    ? '<i class="fa-solid fa-shield"></i> Confirm and apply'
                    : 'Save draft';
            }
            buttons.forEach((button) => { button.disabled = false; });
            UI.showToast(state.riskEditor.error, 'error');
        }
    };

    Actions.runRiskManagerCycle = async function () {
        const liveRegion = document.getElementById('agents-live-region');
        try {
            const response = await API.fetchWithTimeout('/api/agents/risk/run-cycle', {
                method: 'POST',
                timeout: 15000,
                bypassCooldown: true,
            });
            const result = await responseJson(response, 'Could not schedule a Risk Manager analysis cycle.');
            const requestId = result?.request?.request_id;
            UI.showToast('Risk Manager cycle triggered. Scanner, Watchlist, and Pre-Earnings candidates are being evaluated.', 'success');
            if (liveRegion) liveRegion.textContent = `Risk Manager evaluation cycle scheduled${requestId ? ` (${requestId})` : ''}.`;
            window.setTimeout(() => UI.renderAgents({ force: true }), 2500);
            window.setTimeout(() => UI.renderAgents({ force: true }), 6000);
        } catch (error) {
            console.error('Risk Manager cycle request failed:', error);
            UI.showToast(error.message || 'Could not schedule Risk Manager cycle.', 'error');
            if (liveRegion) liveRegion.textContent = error.message || 'Could not schedule Risk Manager cycle.';
        }
    };

    document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape' && state.riskEditor.open && !state.riskEditor.saving) {
            Actions.closeRiskSettings();
        }
    });
})();
