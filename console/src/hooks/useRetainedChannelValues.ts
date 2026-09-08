import { useEffect, useState } from "react";

/** Keep configured channels selectable after a rule or selection is removed. */
export function useRetainedChannelValues(values: readonly string[]): string[] {
  const serialized = JSON.stringify(values);
  const [retained, setRetained] = useState<string[]>(() => [
    ...new Set(values),
  ]);
  useEffect(() => {
    const current: string[] = JSON.parse(serialized);
    setRetained((previous) => {
      const next = [...new Set([...previous, ...current])].filter(Boolean);
      return next.length === previous.length ? previous : next;
    });
  }, [serialized]);
  return [...new Set([...retained, ...values])].filter(Boolean);
}
