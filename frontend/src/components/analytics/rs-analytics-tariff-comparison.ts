/** Compare logged room heating with observed tariffs over two periods. */
import { LitElement, css, html, nothing } from "lit";
import { customElement, property, state } from "lit/decorators.js";
import type { HomeAssistant } from "../../types";
import { localize } from "../../utils/localize";

type Band = "cheap" | "typical" | "expensive";
type PeriodKey = "current" | "comparison";
type DateRange = { from: string; to: string };

interface TariffUnitStats {
  unit: string | null;
  priced_heating_seconds: number;
  average_tariff: number | null;
  band_seconds: Record<Band, number>;
}

interface TariffSegment {
  start_ts: number;
  end_ts: number;
  price: number | null;
  unit: string | null;
  heating: boolean;
}

interface TariffPeriod {
  start_ts: number;
  end_ts: number;
  monitored_seconds: number;
  heating_seconds: number;
  units: TariffUnitStats[];
  segments: TariffSegment[];
}

interface TariffComparison {
  periods: [TariffPeriod, TariffPeriod];
  cutoffs: [number, number] | null;
}

const DAYS_PER_RANGE = 7;
const ROWS_PER_PAGE = 20;
const BANDS: Band[] = ["cheap", "typical", "expensive"];

function relativeDate(days: number): string {
  const date = new Date();
  date.setDate(date.getDate() + days);
  return [
    date.getFullYear(),
    String(date.getMonth() + 1).padStart(2, "0"),
    String(date.getDate()).padStart(2, "0"),
  ].join("-");
}

function toTimestamps(range: DateRange) {
  const start = new Date(`${range.from}T00:00:00`);
  const end = new Date(`${range.to}T00:00:00`);
  // Use calendar days so the final date is inclusive even across a clock change.
  end.setDate(end.getDate() + 1);
  return { start_ts: start.getTime() / 1000, end_ts: end.getTime() / 1000 };
}

@customElement("rs-analytics-tariff-comparison")
export class RsAnalyticsTariffComparison extends LitElement {
  @property({ attribute: false }) public hass!: HomeAssistant;
  @property({ type: String }) public areaId = "";

  @state() private _ranges: Record<PeriodKey, DateRange> = {
    current: { from: relativeDate(1 - DAYS_PER_RANGE), to: relativeDate(0) },
    comparison: { from: relativeDate(1 - DAYS_PER_RANGE * 2), to: relativeDate(-DAYS_PER_RANGE) },
  };
  @state() private _result: TariffComparison | null = null;
  @state() private _loading = false;
  @state() private _error = false;
  @state() private _unit: string | null = null;
  @state() private _pages = [0, 0];
  private _request = 0;

  protected willUpdate(changed: Map<string, unknown>) {
    if (changed.has("areaId")) {
      this._request++;
      this._result = null;
      this._loading = false;
      this._error = false;
    }
  }

  protected render() {
    const l = this.hass.language;
    const units = this._units();
    return html`
      <ha-card>
        <details>
          <summary>${localize("tariff_analytics.title", l)}</summary>
          <div class="body">
            <p>${localize("tariff_analytics.help", l)}</p>
            <form @submit=${this._compare}>
              <div class="fields">
                ${this._renderRange("current")} ${this._renderRange("comparison")}
                <fieldset>
                  <legend>${localize("tariff_analytics.bands", l)}</legend>
                  <label>
                    ${localize("tariff_analytics.cheap_max", l)}
                    <input type="number" name="cheap_max" step="any" />
                  </label>
                  <label>
                    ${localize("tariff_analytics.expensive_min", l)}
                    <input type="number" name="expensive_min" step="any" />
                  </label>
                </fieldset>
              </div>
              <button type="submit" ?disabled=${this._loading}>
                ${localize(this._loading ? "panel.loading" : "tariff_analytics.compare", l)}
              </button>
            </form>
            ${
              this._error
                ? html`<p role="alert">${localize("tariff_analytics.error", l)}</p>`
                : nothing
            }
            ${
              this._result
                ? html`
                    ${
                      units.length > 1
                        ? html`
                            <label class="unit">
                              ${localize("tariff_analytics.unit", l)}
                              <select
                                .value=${String(units.indexOf(this._unit))}
                                @change=${(event: Event) => {
                                  this._unit =
                                    units[Number((event.target as HTMLSelectElement).value)];
                                  this._pages = [0, 0];
                                }}
                              >
                                ${units.map(
                                  (unit, index) => html`
                                    <option value=${index} ?selected=${unit === this._unit}>
                                      ${unit ?? localize("tariff_analytics.unknown_unit", l)}
                                    </option>
                                  `,
                                )}
                              </select>
                            </label>
                          `
                        : nothing
                    }
                    <div class="results">
                      ${this._result.periods.map((period, index) => this._renderPeriod(period, index))}
                    </div>
                  `
                : nothing
            }
          </div>
        </details>
      </ha-card>
    `;
  }

  private _renderRange(key: PeriodKey) {
    const l = this.hass.language;
    const setDate = (field: keyof DateRange, event: Event) => {
      this._ranges = {
        ...this._ranges,
        [key]: { ...this._ranges[key], [field]: (event.target as HTMLInputElement).value },
      };
    };
    return html`
      <fieldset>
        <legend>${localize(`tariff_analytics.${key}`, l)}</legend>
        <label>
          ${localize("analytics.range_from", l)}
          <input
            type="date"
            .value=${this._ranges[key].from}
            @change=${(e: Event) => setDate("from", e)}
            required
          />
        </label>
        <label>
          ${localize("analytics.range_to", l)}
          <input
            type="date"
            .value=${this._ranges[key].to}
            @change=${(e: Event) => setDate("to", e)}
            required
          />
        </label>
      </fieldset>
    `;
  }

  private async _compare(event: SubmitEvent) {
    event.preventDefault();
    if (this._loading || !this.areaId) return;

    const form = new FormData(event.currentTarget as HTMLFormElement);
    const low = form.get("cheap_max");
    const high = form.get("expensive_min");
    const current = toTimestamps(this._ranges.current);
    const comparison = toTimestamps(this._ranges.comparison);
    const hasCutoffs = low !== "" || high !== "";

    if (
      !(current.end_ts > current.start_ts && comparison.end_ts > comparison.start_ts) ||
      (hasCutoffs && (low === "" || high === "" || !(Number(low) < Number(high))))
    ) {
      this._error = true;
      return;
    }

    const request = ++this._request;
    this._loading = true;
    this._error = false;
    try {
      const result = await this.hass.callWS<TariffComparison>({
        type: "roommind/analytics/tariffs",
        area_id: this.areaId,
        current,
        comparison,
        ...(hasCutoffs ? { cheap_max: Number(low), expensive_min: Number(high) } : {}),
      });
      if (request !== this._request) return;
      this._result = result;
      const units = this._units();
      if (!units.includes(this._unit)) this._unit = units[0] ?? null;
      this._pages = [0, 0];
    } catch {
      if (request === this._request) this._error = true;
    } finally {
      if (request === this._request) this._loading = false;
    }
  }

  private _units(): (string | null)[] {
    return [
      ...new Set(this._result?.periods.flatMap((period) => period.units.map((unit) => unit.unit))),
    ];
  }

  private _renderPeriod(period: TariffPeriod, index: number) {
    const l = this.hass.language;
    const stats = period.units.find((entry) => entry.unit === this._unit);
    const hours = (seconds: number) =>
      `${new Intl.NumberFormat(l, { maximumFractionDigits: 2 }).format(seconds / 3600)} h`;
    const tariff = (price: number | null) =>
      price === null
        ? "—"
        : new Intl.NumberFormat(l, { maximumSignificantDigits: 15 }).format(price);
    const date = (ts: number) =>
      new Intl.DateTimeFormat(l, {
        month: "short",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit",
        timeZoneName: "shortOffset",
      }).format(ts * 1000);
    const unit = this._unit ?? (stats ? localize("tariff_analytics.unknown_unit", l) : "");
    const segments = period.segments.filter(
      (segment) => segment.price === null || segment.unit === this._unit,
    );
    const page = this._pages[index];
    const offset = page * ROWS_PER_PAGE;

    return html`
      <section>
        <h3>
          ${localize(index === 0 ? "tariff_analytics.current" : "tariff_analytics.comparison", l)}
        </h3>
        <p class="range">${date(period.start_ts)} – ${date(period.end_ts)}</p>
        <dl>
          <dt>${localize("tariff_analytics.monitored", l)}</dt>
          <dd>${hours(period.monitored_seconds)}</dd>
          <dt>${localize("analytics.heating_period", l)}</dt>
          <dd>${hours(period.heating_seconds)}</dd>
          <dt>${localize("tariff_analytics.priced_heating", l)}</dt>
          <dd>${hours(stats?.priced_heating_seconds ?? 0)}</dd>
          <dt>${localize("tariff_analytics.average", l)}</dt>
          <dd>
            ${stats?.average_tariff != null ? `${tariff(stats.average_tariff)} ${unit}` : "—"}
          </dd>
          ${
            this._result?.cutoffs && stats
              ? BANDS.map(
                  (band) => html`
                    <dt>${localize(`tariff_analytics.${band}`, l)}</dt>
                    <dd>${hours(stats.band_seconds[band])}</dd>
                  `,
                )
              : nothing
          }
        </dl>
        ${!stats ? html`<p>${localize("tariff_analytics.no_prices", l)}</p>` : nothing}
        ${
          segments.length
            ? html`
                <details class="observations">
                  <summary>${localize("tariff_analytics.observations", l)}</summary>
                  <div class="table-scroll">
                    <table>
                      <thead>
                        <tr>
                          <th scope="col">${localize("analytics.range_from", l)}</th>
                          <th scope="col">${localize("analytics.range_to", l)}</th>
                          <th scope="col">${localize("tariff_analytics.price", l)}</th>
                          <th scope="col">${localize("analytics.heating_period", l)}</th>
                        </tr>
                      </thead>
                      <tbody>
                        ${segments.slice(offset, offset + ROWS_PER_PAGE).map(
                          (segment) => html`
                            <tr>
                              <td>${date(segment.start_ts)}</td>
                              <td>${date(segment.end_ts)}</td>
                              <td>
                                ${tariff(segment.price)} ${segment.price !== null ? unit : ""}
                              </td>
                              <td>
                                ${segment.heating ? hours(segment.end_ts - segment.start_ts) : "—"}
                              </td>
                            </tr>
                          `,
                        )}
                      </tbody>
                    </table>
                  </div>
                  ${
                    segments.length > ROWS_PER_PAGE
                      ? html`<div class="pager">
                          <button
                            type="button"
                            ?disabled=${page === 0}
                            @click=${() => this._turnPage(index, -1)}
                          >
                            ${localize("tariff_analytics.previous", l)}
                          </button>
                          <span
                            >${offset + 1}–${Math.min(offset + ROWS_PER_PAGE, segments.length)} /
                            ${segments.length}</span
                          >
                          <button
                            type="button"
                            ?disabled=${offset + ROWS_PER_PAGE >= segments.length}
                            @click=${() => this._turnPage(index, 1)}
                          >
                            ${localize("tariff_analytics.next", l)}
                          </button>
                        </div>`
                      : nothing
                  }
                </details>
              `
            : nothing
        }
      </section>
    `;
  }

  private _turnPage(index: number, increment: number) {
    this._pages = this._pages.map((page, i) => (i === index ? page + increment : page));
  }

  static styles = css`
    :host {
      display: block;
      margin-top: 16px;
    }
    summary {
      cursor: pointer;
      font-weight: 600;
    }
    ha-card > details > summary {
      padding: 16px;
    }
    .body {
      padding: 0 16px 16px;
    }
    p {
      color: var(--secondary-text-color);
    }
    .fields,
    .results {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(min(100%, 220px), 1fr));
      gap: 12px;
      margin: 12px 0;
    }
    fieldset,
    section {
      border: 1px solid var(--divider-color);
      border-radius: 8px;
      min-width: 0;
      padding: 12px;
    }
    label {
      display: flex;
      flex-direction: column;
      gap: 4px;
      margin: 4px 0;
    }
    input,
    select,
    button {
      font: inherit;
      color: var(--primary-text-color);
      background: var(--secondary-background-color);
      border: 1px solid var(--divider-color);
      border-radius: 6px;
      padding: 6px 8px;
    }
    button {
      cursor: pointer;
    }
    button:disabled {
      opacity: 0.5;
      cursor: default;
    }
    h3 {
      margin: 0;
    }
    .range {
      font-size: 0.85em;
    }
    dl {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 4px 12px;
    }
    dd {
      margin: 0;
      text-align: right;
    }
    .unit {
      max-width: 220px;
      margin-top: 12px;
    }
    .observations {
      margin-top: 12px;
    }
    .table-scroll {
      overflow-x: auto;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 0.85em;
    }
    th,
    td {
      text-align: left;
      padding: 6px;
      border-bottom: 1px solid var(--divider-color);
    }
    .pager {
      display: flex;
      gap: 6px;
      align-items: center;
      justify-content: space-between;
      margin-top: 8px;
    }
  `;
}

declare global {
  interface HTMLElementTagNameMap {
    "rs-analytics-tariff-comparison": RsAnalyticsTariffComparison;
  }
}
