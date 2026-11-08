/** Read-only comparison of two schedules on the same recorded conditions. */
import { LitElement, css, html, nothing } from "lit";
import { customElement, property, state } from "lit/decorators.js";
import type { HomeAssistant } from "../../types";
import { localize, type TranslationKey } from "../../utils/localize";
import { tempRange, tempStep, tempUnit, toCelsius, toDisplayDelta } from "../../utils/temperature";

interface Scenario {
  too_cold_minutes: number;
  too_warm_minutes: number;
  heating_minutes: number;
  tariff_minutes: number;
  average_heating_tariff: number | null;
  tariff_unit: string;
}

interface Comparison {
  current: Scenario;
  proposed: Scenario;
  start_ts: number;
  end_ts: number;
  simulated_minutes: number;
  comfort_minutes: number;
  tariff_minutes: number;
  heating_by_tariff: Array<{ price: number; unit: string; current: number; proposed: number }>;
}

const errorKeys: Record<string, TranslationKey> = {
  invalid_input: "analytics.simulation.invalid_input",
  room_unavailable: "analytics.simulation.room_unavailable",
  history_missing: "analytics.simulation.history_missing",
  model_untrained: "analytics.simulation.model_untrained",
  schedule_missing: "analytics.simulation.schedule_missing",
};

@customElement("rs-schedule-simulation")
export class RsScheduleSimulation extends LitElement {
  @property({ attribute: false }) public hass!: HomeAssistant;
  @property() public areaId = "";
  @property({ type: Number }) public rangeStart = 0;
  @property({ type: Number }) public rangeEnd = 0;

  @state() private _shift = "";
  @state() private _overnight = "";
  @state() private _daytime = "";
  @state() private _loading = false;
  @state() private _comparison: Comparison | null = null;
  @state() private _error: TranslationKey | null = null;

  private _requestId = 0;

  protected willUpdate(changed: Map<string, unknown>) {
    if (changed.has("areaId") || changed.has("rangeStart") || changed.has("rangeEnd")) {
      this._requestId++;
      this._comparison = null;
      this._error = null;
      this._loading = false;
    }
    if (changed.has("areaId")) {
      this._shift = "";
      this._overnight = "";
      this._daytime = "";
    }
  }

  protected render() {
    const l = this.hass.language;
    const { min, max } = tempRange(5, 35, this.hass);

    return html`
      <ha-card>
        <div class="content">
          <h2>${localize("analytics.simulation.title", l)}</h2>
          <p>${localize("analytics.simulation.intro", l)}</p>
          <form @submit=${this._run}>
            <fieldset ?disabled=${this._loading}>
              <label>
                ${localize("analytics.simulation.shift", l)}
                <input
                  type="number"
                  min="-180"
                  max="180"
                  step="1"
                  placeholder="0"
                  .value=${this._shift}
                  @input=${(e: Event) => this._edit("_shift", e)}
                />
              </label>
              <label>
                ${localize("analytics.simulation.overnight", l, { unit: tempUnit(this.hass) })}
                <input
                  type="number"
                  min=${min}
                  max=${max}
                  step=${tempStep(this.hass)}
                  .value=${this._overnight}
                  @input=${(e: Event) => this._edit("_overnight", e)}
                />
              </label>
              <label>
                ${localize("analytics.simulation.daytime", l, { unit: tempUnit(this.hass) })}
                <input
                  type="number"
                  min=${min}
                  max=${max}
                  step=${tempStep(this.hass)}
                  .value=${this._daytime}
                  @input=${(e: Event) => this._edit("_daytime", e)}
                />
              </label>
            </fieldset>
            <button type="submit" ?disabled=${this._loading}>
              ${localize(this._loading ? "panel.loading" : "analytics.simulation.run", l)}
            </button>
          </form>
          ${this._error ? html`<p class="error" role="alert">${localize(this._error, l)}</p>` : nothing}
          ${this._comparison ? this._renderComparison(this._comparison) : nothing}
        </div>
      </ha-card>
    `;
  }

  private _edit(field: "_shift" | "_overnight" | "_daytime", e: Event) {
    this[field] = (e.currentTarget as HTMLInputElement).value;
    this._comparison = null;
    this._error = null;
  }

  private async _run(e: Event) {
    e.preventDefault();
    if (this._loading) return;
    const requestId = ++this._requestId;
    this._loading = true;
    this._comparison = null;
    this._error = null;
    try {
      const result = await this.hass.callWS<Comparison>({
        type: "roommind/analytics/simulate_schedule",
        area_id: this.areaId,
        start_ts: this.rangeStart / 1000,
        end_ts: this.rangeEnd / 1000,
        start_shift_minutes: Number(this._shift),
        ...(this._overnight
          ? { overnight_heat: toCelsius(Number(this._overnight), this.hass) }
          : {}),
        ...(this._daytime ? { daytime_heat: toCelsius(Number(this._daytime), this.hass) } : {}),
      });
      if (requestId === this._requestId) this._comparison = result;
    } catch (err: unknown) {
      if (requestId === this._requestId) {
        const code = (err as { code?: string } | null)?.code ?? "";
        this._error = errorKeys[code] ?? "analytics.simulation.failed";
      }
    } finally {
      if (requestId === this._requestId) this._loading = false;
    }
  }

  private _renderComparison(result: Comparison) {
    const l = this.hass.language;
    const date = new Intl.DateTimeFormat(l, { dateStyle: "short", timeStyle: "short" });
    const number = new Intl.NumberFormat(l, { maximumFractionDigits: 4 });
    const formatTariff = (price: number, unit: string) => `${number.format(price)} ${unit}`.trim();
    const averageTariff = ({ average_heating_tariff, tariff_unit }: Scenario) =>
      average_heating_tariff === null ? "—" : formatTariff(average_heating_tariff, tariff_unit);
    const metric = (
      label: TranslationKey,
      current: number | string,
      proposed: number | string,
    ) => html`
      <tr>
        <th scope="row">${localize(label, l)}</th>
        <td>${current}</td>
        <td>${proposed}</td>
      </tr>
    `;
    const headers = (label: TranslationKey) => html`
      <tr>
        <th scope="col">${localize(label, l)}</th>
        <th scope="col">${localize("analytics.simulation.current", l)}</th>
        <th scope="col">${localize("analytics.simulation.proposed", l)}</th>
      </tr>
    `;

    return html`
      <div aria-live="polite">
        <p>${date.format(result.start_ts * 1000)} – ${date.format(result.end_ts * 1000)}</p>
        <p>${localize("analytics.simulation.assumptions", l)}</p>
        <p>
          ${localize("analytics.simulation.coverage", l, {
            simulated: result.simulated_minutes,
            selected: (result.end_ts - result.start_ts) / 60,
            comfort: result.comfort_minutes,
            tolerance: number.format(toDisplayDelta(1, this.hass)),
            unit: tempUnit(this.hass),
          })}
        </p>
        <table>
          <thead>
            ${headers("analytics.simulation.metric")}
          </thead>
          <tbody>
            ${metric("analytics.simulation.cold", result.current.too_cold_minutes, result.proposed.too_cold_minutes)}
            ${metric("analytics.simulation.warm", result.current.too_warm_minutes, result.proposed.too_warm_minutes)}
            ${metric("analytics.simulation.heating", result.current.heating_minutes, result.proposed.heating_minutes)}
            ${
              result.tariff_minutes
                ? html`
                    ${metric("analytics.simulation.tariff_coverage", result.current.tariff_minutes, result.proposed.tariff_minutes)}
                    ${metric("analytics.simulation.average_tariff", averageTariff(result.current), averageTariff(result.proposed))}
                  `
                : nothing
            }
          </tbody>
        </table>
        ${
          result.tariff_minutes
            ? result.heating_by_tariff.length
              ? html`<details>
                  <summary>${localize("analytics.simulation.tariff_details", l)}</summary>
                  <table>
                    <thead>
                      ${headers("analytics.simulation.tariff")}
                    </thead>
                    <tbody>
                      ${result.heating_by_tariff.map(
                        (row) => html`
                          <tr>
                            <th scope="row">${formatTariff(row.price, row.unit)}</th>
                            <td>${row.current}</td>
                            <td>${row.proposed}</td>
                          </tr>
                        `,
                      )}
                    </tbody>
                  </table>
                </details>`
              : nothing
            : html`<p>${localize("analytics.simulation.tariff_missing", l)}</p>`
        }
      </div>
    `;
  }

  static styles = css`
    :host {
      display: block;
      margin-top: 16px;
    }
    .content {
      padding: 16px;
    }
    h2 {
      font-size: 1.2rem;
      margin: 0;
    }
    p {
      color: var(--secondary-text-color);
    }
    fieldset {
      border: 0;
      margin: 0 0 12px;
      padding: 0;
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(min(160px, 100%), 1fr));
      gap: 12px;
    }
    label {
      display: grid;
      gap: 4px;
    }
    input,
    button {
      font: inherit;
      color: inherit;
      padding: 8px;
      border-radius: 6px;
    }
    input {
      width: 100%;
      box-sizing: border-box;
      background: var(--card-background-color);
      border: 1px solid var(--divider-color);
    }
    button {
      cursor: pointer;
      background: var(--primary-color);
      color: var(--text-primary-color);
      border: 0;
    }
    button:disabled {
      opacity: 0.6;
      cursor: default;
    }
    table {
      width: 100%;
      border-collapse: collapse;
    }
    th,
    td {
      text-align: start;
      padding: 6px;
      border-bottom: 1px solid var(--divider-color);
    }
    tbody th {
      font-weight: normal;
    }
    details {
      margin-top: 12px;
    }
    summary {
      cursor: pointer;
    }
    .error {
      color: var(--error-color);
    }
  `;
}

declare global {
  interface HTMLElementTagNameMap {
    "rs-schedule-simulation": RsScheduleSimulation;
  }
}
