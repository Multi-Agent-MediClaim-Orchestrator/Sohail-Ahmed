import Decimal from "decimal.js";

/** ₹ with Indian (lakh) grouping. Amounts stay strings end to end; Decimal is only for display and totals. */
export function inr(v: string | number | null | undefined): string {
  if (v === null || v === undefined || v === "") return "—";
  const d = new Decimal(v);
  const [i, f = "00"] = d.toFixed(2).split(".");
  const neg = i.startsWith("-");
  const digits = neg ? i.slice(1) : i;
  const last3 = digits.slice(-3);
  const rest = digits.slice(0, -3).replace(/\B(?=(\d{2})+(?!\d))/g, ",");
  return `${neg ? "-" : ""}₹${rest ? rest + "," : ""}${last3}.${f}`;
}
export function sumAmounts(xs: string[]): string {
  return xs.reduce((a, x) => a.plus(x || 0), new Decimal(0)).toFixed(2);
}
export function isAmount(s: string): boolean { return /^\d{1,12}(\.\d{1,2})?$/.test(s.trim()); }
export function minus(a: string, b: string): string {
  const d = new Decimal(a || 0).minus(b || 0);
  return (d.isNegative() ? new Decimal(0) : d).toFixed(2);
}
