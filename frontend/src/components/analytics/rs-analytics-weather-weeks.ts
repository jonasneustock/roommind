import { LitElement, css, html, nothing } from "lit";
import { customElement, property, state } from "lit/decorators.js";
import type { HomeAssistant } from "../../types";
import { localize } from "../../utils/localize";
import { tempUnit, toDisplay, toDisplayDelta } from "../../utils/temperature";

interface WeekPeriod {
  start_ts: number;
  end_ts: number;
}

interface WeatherWeekMatch {
  status: "matched" | "insufficient_current" | "no_comparable_week";
  current: WeekPeriod;
  comparison: WeekPeriod | null;
  time_zone: string;
  weather_mae: number | null;
  matched_hours: number;
  expected_hours: number;
  outdoor_profile: {
    ts: number;
    current_temp: number | null;
    comparison_temp: number | null;
  }[];
}

@customElement("rs-analytics-weather-weeks")
export class RsAnalyticsWeatherWeeks extends LitElement {
  @property({ attribute: false }) public hass!: HomeAssistant;
  @state() private _match: WeatherWeekMatch | null = null;
  @state() private _loading = false;
  @state() private _failed = false;

  protected firstUpdated() {
    void this._loadMatch();
  }

  private async _loadMatch() {
    this._loading = true;
    this._failed = false;
    try {
      this._match = await this.hass.callWS<WeatherWeekMatch>({
        type: "roommind/analytics/weather_match",
      });
    } catch {
      this._match = null;
      this._failed = true;
    } finally {
      this._loading = false;
    }
  }

  private _periodLabel(period: WeekPeriod, zone: string) {
    const format = new Intl.DateTimeFormat(this.hass.language, {
      dateStyle: "medium",
      timeStyle: "short",
      timeZone: zone,
    });
    return format.formatRange(period.start_ts * 1000, period.end_ts * 1000);
  }

  private _viewPeriod(period: WeekPeriod) {
    // The comparison end is exclusive. The existing analytics range is inclusive.
    const rangeEnd = period.end_ts * 1000 - 1;
    this.dispatchEvent(
      new CustomEvent("range-changed", {
        detail: {
          activeQuick: null,
          rangeStart: period.start_ts * 1000,
          rangeEnd,
          chartAnchor: rangeEnd,
        },
        bubbles: true,
        composed: true,
      }),
    );
  }

  private _renderProfile(match: WeatherWeekMatch) {
    const l = this.hass.language;
    const time = new Intl.DateTimeFormat(l, {
      timeZone: match.time_zone,
      weekday: "short",
      hour: "numeric",
      minute: "2-digit",
    });
    const names = [
      localize("analytics.weather_week_current", l),
      localize("analytics.weather_week_comparison", l),
    ];
    const theme = getComputedStyle(this);
    const colors = [
      theme.getPropertyValue("--primary-color").trim() || "rgb(33, 150, 243)",
      theme.getPropertyValue("--warning-color").trim() || "rgb(255, 152, 0)",
    ];
    const series = (["current_temp", "comparison_temp"] as const).map((field, i) => ({
      type: "line",
      name: names[i],
      color: colors[i],
      lineStyle: { width: 2, type: i === 0 ? "solid" : "dashed" },
      showSymbol: false,
      connectNulls: false,
      data: match.outdoor_profile.map((point) => {
        const value = point[field];
        return value == null ? null : Number(toDisplay(value, this.hass).toFixed(1));
      }),
    }));
    return html`<ha-chart-base
      .hass=${this.hass}
      .data=${series}
      .options=${{
        tooltip: { trigger: "axis" },
        legend: { data: names, bottom: 0 },
        grid: { left: 46, right: 14, top: 24, bottom: 62 },
        xAxis: {
          type: "category",
          boundaryGap: false,
          data: match.outdoor_profile.map((point) => time.format(point.ts * 1000)),
          axisLabel: { hideOverlap: true },
        },
        yAxis: { type: "value", name: tempUnit(this.hass), scale: true },
      }}
      .height=${"240px"}
    ></ha-chart-base>`;
  }

  protected render() {
    const l = this.hass.language;
    const match = this._match;
    const comparison = match?.comparison;
    const label = this._failed
      ? "analytics.weather_week_error"
      : match?.status === "no_comparable_week"
        ? "analytics.weather_week_no_match"
        : "analytics.weather_week_insufficient";
    return html`
      <ha-card>
        <div class="weather-header">
          <h2>${localize("analytics.weather_week_title", l)}</h2>
          <ha-button appearance="plain" .disabled=${this._loading} @click=${this._loadMatch}>
            ${localize("analytics.weather_week_refresh", l)}
          </ha-button>
        </div>
        ${
          this._loading
            ? html`<p role="status">${localize("panel.loading", l)}</p>`
            : match && comparison && match.weather_mae != null
              ? html`
                  <dl>
                    <div>
                      <dt>${localize("analytics.weather_week_current", l)}</dt>
                      <dd>${this._periodLabel(match.current, match.time_zone)}</dd>
                      <ha-button appearance="plain" @click=${() => this._viewPeriod(match.current)}>
                        ${localize("analytics.weather_week_view_current", l)}
                      </ha-button>
                    </div>
                    <div>
                      <dt>${localize("analytics.weather_week_comparison", l)}</dt>
                      <dd>${this._periodLabel(comparison, match.time_zone)}</dd>
                      <ha-button appearance="plain" @click=${() => this._viewPeriod(comparison)}>
                        ${localize("analytics.weather_week_view_comparison", l)}
                      </ha-button>
                    </div>
                  </dl>
                  <p>
                    ${localize("analytics.weather_week_quality", l, {
                      difference: new Intl.NumberFormat(l, { maximumFractionDigits: 2 }).format(
                        toDisplayDelta(match.weather_mae, this.hass),
                      ),
                      unit: tempUnit(this.hass),
                      matched: match.matched_hours,
                      total: match.expected_hours,
                    })}
                  </p>
                  ${this._renderProfile(match)}
                `
              : html`<p role="status">${localize(label, l)}</p>`
        }
        ${match && comparison ? html`<p class="caption">${localize("analytics.weather_week_caption", l)}</p>` : nothing}
      </ha-card>
    `;
  }

  static styles = css`
    :host {
      display: block;
    }
    ha-card {
      padding: 16px;
    }
    .weather-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 8px;
    }
    h2 {
      margin: 0;
      font-size: 1.1rem;
    }
    dl {
      display: flex;
      flex-wrap: wrap;
      gap: 12px 24px;
      margin: 12px 0;
    }
    dt,
    .caption {
      color: var(--secondary-text-color);
    }
    dd {
      margin: 4px 0 0;
    }
    p {
      margin: 12px 0 0;
    }
  `;
}

declare global {
  interface HTMLElementTagNameMap {
    "rs-analytics-weather-weeks": RsAnalyticsWeatherWeeks;
  }
}
