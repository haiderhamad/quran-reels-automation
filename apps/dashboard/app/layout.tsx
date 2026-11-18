import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Quran Reels Operations Dashboard",
  description: "Private operational management for automated Quran Reels generation and publishing.",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
