/** Ranks rooms against their recorded schedules for the Analytics date range. */
import { LitElement, css, html } from "lit";
import { customElement, property, state } from "lit/decorators.js";
import type { PropertyValues } from "lit";
import type { HomeAssistant, RoomConfig } from "../../types";
import { localize } from "../../utils/localize";
import { tempUnit, toDisplayDelta } from "../../utils/temperature";

interface ComfortTime {
  valid_seconds: number;
  cold_seconds: number;
  warm_seconds: number;
  miss_pct: number | null;
}

interface ComfortDay extends ComfortTime {
  date: string;
  start_ts: number;
  end_ts: number;
}

interface ComfortRanking {
  start_ts: number;
  end_ts: number;
  rooms: (ComfortTime & { area_id: string; days: ComfortDay[] })[];
}

export interface ComfortDaySelection {
  areaId: string;
  rangeStart: number;
  rangeEnd: number;
}

@customElement("rs-analytics-comfort-ranking")
export class RsAnalyticsComfortRanking extends LitElement {
  @property({ attribute: false }) public hass!: HomeAssistant;
  @property({ attribute: false }) public rooms: Record<string, RoomConfig> = {};
  @property({ type: Number }) public rangeStart = 0;
  @property({ type: Number }) public rangeEnd = 0;

  @state() private _result: ComfortRanking | null = null;
  @state() private _loading = false;
  @state() private _failed = false;
  private _requestId = 0;

  protected updated(changed: PropertyValues) {
    if (changed.has("rangeStart") || changed.has("rangeEnd")) {
      void this._fetchRanking();
    }
  }

  private async _fetchRanking() {
    const requestId = ++this._requestId;
    this._result = null;
    this._failed = false;
    this._loading = !!this.hass && this.rangeEnd > this.rangeStart;
    if (!this._loading) return;

    try {
      const result = await this.hass.callWS<ComfortRanking>({
        type: "roommind/analytics/comfort_ranking",
        start_ts: this.rangeStart / 1000,
        end_ts: this.rangeEnd / 1000,
      });
      if (requestId === this._requestId) this._result = result;
    } catch {
      if (requestId === this._requestId) this._failed = true;
    } finally {
      if (requestId === this._requestId) this._loading = false;
    }
  }

  private _openDay(areaId: string, day: ComfortDay) {
    this.dispatchEvent(
      new CustomEvent<ComfortDaySelection>("comfort-day-selected", {
        detail: { areaId, rangeStart: day.start_ts * 1000, rangeEnd: day.end_ts * 1000 },
        bubbles: true,
        composed: true,
      }),
    );
  }

  protected render() {
    const l = this.hass.language;
    const num = new Intl.NumberFormat(l, { maximumFractionDigits: 2 });
    const hours = (seconds: number) =>
      localize("analytics.comfort_ranking.hours", l, { hours: num.format(seconds / 3600) });
    const date = new Intl.DateTimeFormat(l, { dateStyle: "medium", timeZone: "UTC" });
    const percent = new Intl.NumberFormat(l, { style: "percent", maximumFractionDigits: 1 });
    const ranking = this._result;
    const hasData = ranking?.rooms.some((room) => room.miss_pct !== null);
    return html`
      <ha-card>
        <h2>${localize("analytics.comfort_ranking.title", l)}</h2>
        <p class="hint">
          ${localize("analytics.comfort_ranking.definition", l, {
            tolerance: toDisplayDelta(1, this.hass).toFixed(1),
            unit: tempUnit(this.hass),
          })}
        </p>
        ${
          this._loading
            ? html`<p role="status">${localize("panel.loading", l)}</p>`
            : this._failed
              ? html`<p role="status">${localize("analytics.comfort_ranking.error", l)}</p>`
              : hasData && ranking
                ? ranking.rooms.map((room) => {
                    const name =
                      this.rooms[room.area_id]?.display_name ||
                      this.hass.areas?.[room.area_id]?.name ||
                      room.area_id;
                    if (room.miss_pct === null) {
                      return html`<div class="room no-coverage">
                        <strong>${name}</strong>
                        <span>${localize("analytics.comfort_ranking.no_room_data", l)}</span>
                      </div>`;
                    }
                    return html`<details class="room">
                      <summary>
                        <strong>${name}</strong>
                        <span
                          >${localize("analytics.comfort_ranking.missed", l, {
                            percent: percent.format(room.miss_pct / 100),
                          })}</span
                        >
                        <span
                          >${localize("analytics.comfort_ranking.cold", l, {
                            time: hours(room.cold_seconds),
                          })}</span
                        >
                        <span
                          >${localize("analytics.comfort_ranking.warm", l, {
                            time: hours(room.warm_seconds),
                          })}</span
                        >
                        <span class="hint"
                          >${localize("analytics.comfort_ranking.coverage", l, {
                            time: hours(room.valid_seconds),
                            percent: percent.format(
                              room.valid_seconds / (ranking.end_ts - ranking.start_ts),
                            ),
                          })}</span
                        >
                      </summary>
                      <ul>
                        ${[...room.days].reverse().map(
                          (day) =>
                            html`<li>
                              <button
                                type="button"
                                @click=${() => this._openDay(room.area_id, day)}
                              >
                                ${date.format(new Date(day.date + "T00:00:00Z"))}
                              </button>
                              <span
                                >${localize("analytics.comfort_ranking.cold", l, {
                                  time: hours(day.cold_seconds),
                                })}</span
                              >
                              <span
                                >${localize("analytics.comfort_ranking.warm", l, {
                                  time: hours(day.warm_seconds),
                                })}</span
                              >
                            </li>`,
                        )}
                      </ul>
                    </details>`;
                  })
                : html`<p role="status">${localize("analytics.comfort_ranking.no_data", l)}</p>`
        }
      </ha-card>
    `;
  }

  static styles = css`
    ha-card {
      margin: 16px 0;
      padding: 16px;
    }

    h2 {
      font-size: 1.1rem;
      margin: 0 0 8px;
    }

    .hint,
    .no-coverage span {
      color: var(--secondary-text-color);
    }

    .room {
      border-top: 1px solid var(--divider-color);
      padding: 8px 0;
    }

    summary,
    button {
      cursor: pointer;
    }

    strong,
    span {
      display: inline-block;
      margin-inline-end: 12px;
    }

    li {
      margin-block: 4px;
    }

    button {
      font: inherit;
      color: var(--primary-text-color);
      background: none;
      border: 0;
      text-decoration: underline;
      margin-inline-end: 8px;
    }
  `;
}

declare global {
  interface HTMLElementTagNameMap {
    "rs-analytics-comfort-ranking": RsAnalyticsComfortRanking;
  }
}
