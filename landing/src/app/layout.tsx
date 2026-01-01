import type { Metadata } from "next";
import localFont from "next/font/local";
import "computer-modern/cmu-classical-serif.css";
import "./globals.css";

const suit = localFont({
  src: "../../public/fonts/SUIT-Variable.woff2",
  variable: "--font-suit",
  display: "swap",
});

export const metadata: Metadata = {
  title: "AI 랠리와 금 폭주 - 2025년 12월 22일 장마감 브리핑",
  description: "AI 랠리와 금 폭주, 위험과 안전이 동시에 달린 날. 매일 아침, AI가 전날 미국 증시의 주요 흐름과 핵심 종목 동향을 분석해 팟캐스트로 전달합니다.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="ko">
      <body className={`${suit.variable} antialiased`}>
        {children}
      </body>
    </html>
  );
}
