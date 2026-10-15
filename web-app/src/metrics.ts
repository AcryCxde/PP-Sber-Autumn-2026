import type { Metric, MetricName } from "./types.ts";
// Session-only, metadata only. A deployment can subscribe; there is no analytics endpoint.
const recent: Metric[] = [];
export function emitMetric(
  name: MetricName,
  fields: Omit<Metric, "name" | "at"> = {},
) {
  const buildVersion = (import.meta as ImportMeta & { env?: { VITE_BUILD_VERSION?: string } }).env?.VITE_BUILD_VERSION ?? "0.1.0";
  const event = { name, at: new Date().toISOString(), buildVersion, ...fields };
  recent.push(event);
  if (recent.length > 100) recent.shift();
  if (typeof window !== "undefined")
    window.dispatchEvent(
      new CustomEvent("coreteams:metric", { detail: event }),
    );
}
export const sessionMetrics = () => [...recent];
