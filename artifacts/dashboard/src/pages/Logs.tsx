import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef } from "react";

const BASE = import.meta.env.BASE_URL.replace(/\/$/, "");

type LogResponse = { lines: string[]; total: number };

function colorLine(line: string) {
  if (line.includes("ERROR") || line.includes("error")) return "text-red-400";
  if (line.includes("WARNING") || line.includes("WARN")) return "text-yellow-400";
  if (line.includes("✅") || line.includes("🟢") || line.includes("WIN") || line.includes("profit")) return "text-green-400";
  if (line.includes("🔴") || line.includes("LOSS")) return "text-red-400";
  if (line.includes("INFO")) return "text-blue-300";
  if (line.includes("🚀")) return "text-purple-400";
  return "text-muted-foreground";
}

export function Logs() {
  const bottomRef = useRef<HTMLDivElement>(null);

  const { data, isLoading } = useQuery<LogResponse>({
    queryKey: ["bot-logs"],
    queryFn: async () => {
      const r = await fetch(`${BASE}/api/bot/logs`);
      return r.json();
    },
    refetchInterval: 3000,
  });

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [data?.lines?.length]);

  return (
    <div className="p-6 h-full flex flex-col space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-xl font-bold">Bot Logs</h2>
          <p className="text-sm text-muted-foreground">
            Live Log-Ausgabe — {data?.total ?? 0} Zeilen — aktualisiert alle 3s
          </p>
        </div>
        <div className="flex items-center gap-2">
          <div className="w-2 h-2 rounded-full bg-green-400 animate-pulse" />
          <span className="text-xs text-muted-foreground">Live</span>
        </div>
      </div>

      <div className="flex-1 bg-black/50 rounded-lg border border-border p-4 overflow-auto font-mono text-xs min-h-0">
        {isLoading && <p className="text-muted-foreground">Logs werden geladen...</p>}

        {data && data.lines.length === 0 && (
          <p className="text-muted-foreground">
            Keine Logs gefunden. Starte den Bot, um Logs zu sehen.
          </p>
        )}

        {data?.lines.map((line, i) => (
          <div key={i} className={`leading-5 hover:bg-white/5 px-1 rounded ${colorLine(line)}`}>
            {line || "\u00A0"}
          </div>
        ))}
        <div ref={bottomRef} />
      </div>
    </div>
  );
}
