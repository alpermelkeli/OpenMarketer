import type { Metadata } from "next";
import { Geist_Mono, Hedvig_Letters_Sans, Hedvig_Letters_Serif } from "next/font/google";

import { AppShell } from "@/components/shell/app-shell";

import "./globals.css";
import { Providers } from "./providers";

const sans = Hedvig_Letters_Sans({
  variable: "--font-hedvig-sans",
  subsets: ["latin"],
  weight: "400",
});

const serif = Hedvig_Letters_Serif({
  variable: "--font-hedvig-serif",
  subsets: ["latin"],
  weight: "400",
});

const mono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: { default: "OpenMarketer", template: "%s · OpenMarketer" },
  description: "Review what OpenMarketer learned about your product before it is used.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className={`${sans.variable} ${serif.variable} ${mono.variable} h-full antialiased`}>
      <body className="min-h-full">
        <Providers>
          <AppShell>{children}</AppShell>
        </Providers>
      </body>
    </html>
  );
}
