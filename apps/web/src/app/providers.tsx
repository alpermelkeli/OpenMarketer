"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";

type ProvidersProps = { children: ReactNode };

export function Providers({ children }: ProvidersProps) {
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          // The API is on this machine and the data changes only when someone acts,
          // so a short freshness window avoids refetching on every navigation.
          queries: { staleTime: 10_000, refetchOnWindowFocus: true },
        },
      }),
  );
  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
}
