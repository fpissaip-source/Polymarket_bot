import { useState } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Overview } from "@/pages/Overview";
import { Markets } from "@/pages/Markets";
import { Trades } from "@/pages/Trades";
import { Logs } from "@/pages/Logs";
import { BotControl } from "@/pages/BotControl";
import { cn } from "@/lib/utils";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      staleTime: 2000,
    },
  },
});

type Page = "overview" | "markets" | "trades" | "logs" | "control";

const NAV_ITEMS: { id: Page; label: string; icon: string }[] = [
  { id: "overview", label: "Übersicht", icon: "📊" },
  { id: "markets", label: "Märkte", icon: "📈" },
  { id: "trades", label: "Trades", icon: "💹" },
  { id: "logs", label: "Logs", icon: "📋" },
  { id: "control", label: "Bot Steuerung", icon: "🤖" },
];

function Dashboard() {
  const [page, setPage] = useState<Page>("overview");

  return (
    <div className="flex h-screen bg-background text-foreground overflow-hidden">
      <aside className="w-56 border-r border-border flex flex-col bg-card shrink-0">
        <div className="p-4 border-b border-border">
          <div className="flex items-center gap-2">
            <span className="text-xl">⚡</span>
            <div>
              <h1 className="text-sm font-bold text-foreground">Polymarket Bot</h1>
              <p className="text-xs text-muted-foreground">Trading Dashboard</p>
            </div>
          </div>
        </div>
        <nav className="flex-1 p-2">
          {NAV_ITEMS.map((item) => (
            <button
              key={item.id}
              onClick={() => setPage(item.id)}
              className={cn(
                "w-full flex items-center gap-3 px-3 py-2.5 rounded-md text-sm font-medium transition-colors mb-1",
                page === item.id
                  ? "bg-primary text-primary-foreground"
                  : "text-muted-foreground hover:text-foreground hover:bg-accent"
              )}
            >
              <span>{item.icon}</span>
              <span>{item.label}</span>
            </button>
          ))}
        </nav>
        <div className="p-3 border-t border-border">
          <p className="text-xs text-muted-foreground text-center">v1.0.0 • Dry Run Mode</p>
        </div>
      </aside>

      <main className="flex-1 overflow-auto">
        {page === "overview" && <Overview />}
        {page === "markets" && <Markets />}
        {page === "trades" && <Trades />}
        {page === "logs" && <Logs />}
        {page === "control" && <BotControl />}
      </main>
    </div>
  );
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <Dashboard />
    </QueryClientProvider>
  );
}
