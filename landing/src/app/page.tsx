"use client";

import { motion } from "framer-motion";
import {
  TrendingUp,
  TrendingDown,
  Coins,
  Cpu,
  Film,
  Calendar,
  ArrowUp,
  ArrowDown,
  ChevronDown
} from "lucide-react";

// Slide data extracted from podcast scripts
const slides = [
  {
    id: "title",
    type: "title",
    date: "December 22, 2025",
    dateKo: "2025년 12월 22일",
    headline: "AI 랠리와 금 폭주",
    subheadline: "위험과 안전이 동시에 달린 날",
  },
  {
    id: "overview",
    type: "indices",
    title: "Market Overview",
    subtitle: "3거래일 연속 상승",
    data: [
      { ticker: "S&P 500", change: "+0.6%", positive: true },
      { ticker: "NASDAQ", change: "+0.5%", positive: true },
      { ticker: "DOW", change: "+0.5%", positive: true },
      { ticker: "Russell 2000", change: "+1.0%", positive: true },
    ],
  },
  {
    id: "gold",
    type: "highlight",
    icon: "coins",
    title: "Gold & Silver",
    subtitle: "1979년 이후 최고의 해",
    metrics: [
      { label: "Gold", value: "$4,445", subtext: "/oz", change: "+70%", unit: "YTD" },
      { label: "Silver", value: "$68", subtext: "/oz", change: "+130%", unit: "YTD" },
    ],
    tickers: ["GC=F", "SI=F", "GLD", "GDX", "SIL"],
    theme: "gold",
  },
  {
    id: "drivers",
    type: "factors",
    title: "What's Driving the Rally?",
    factors: [
      { icon: "🌍", label: "Geopolitical Risk", desc: "Venezuela · Ukraine" },
      { icon: "📉", label: "Rate Cut Expectations", desc: "Fed 2x cuts in 2026" },
      { icon: "💵", label: "Dollar Weakness", desc: "Debasement Trade" },
      { icon: "🏦", label: "Central Bank Buying", desc: "5 months inflow" },
    ],
  },
  {
    id: "ai",
    type: "highlight",
    icon: "cpu",
    title: "AI Infrastructure",
    subtitle: "칩과 데이터센터 투자 가속",
    metrics: [
      { label: "Nvidia H200", value: "China", subtext: "Feb shipments", change: "40-80K", unit: "chips" },
      { label: "Alphabet", value: "$4.75B", subtext: "Intersect", change: "DC", unit: "acquisition" },
    ],
    tickers: ["NVDA", "GOOGL", "^IXIC"],
    theme: "blue",
  },
  {
    id: "tech-debt",
    type: "single-stat",
    title: "Tech Debt Issuance",
    value: "$420B",
    subtitle: "Global tech corporate bonds",
    note: "사상 최대 · AI 설비 투자 자금",
    theme: "purple",
  },
  {
    id: "hollywood",
    type: "highlight",
    icon: "film",
    title: "Hollywood Big Deal",
    subtitle: "Warner Bros Discovery 인수전",
    metrics: [
      { label: "Netflix", value: "$27", subtext: "/share", change: "80B", unit: "studio only" },
      { label: "Paramount", value: "$30", subtext: "/share", change: "108B", unit: "hostile bid" },
    ],
    keyPoint: {
      label: "Larry Ellison",
      value: "$40.4B",
      desc: "Personal Guarantee",
    },
    tickers: ["WBD", "NFLX", "PARA", "ORCL"],
    theme: "red",
  },
  {
    id: "tomorrow",
    type: "calendar",
    title: "Tomorrow",
    subtitle: "2025년 12월 23일",
    events: [
      { time: "AM", label: "GDP Growth Rate", desc: "3Q Final" },
      { time: "AM", label: "Core PCE", desc: "연준 목표 물가지표" },
      { time: "PM", label: "2-Year Auction", desc: "국채 입찰" },
    ],
  },
  {
    id: "closing",
    type: "closing",
    headline: "Stock Morning",
    tagline: "Yesterday's close, Today's edge",
  },
];

// Icon component mapper
function IconComponent({ name, className }: { name: string; className?: string }) {
  const icons: Record<string, React.ReactNode> = {
    coins: <Coins className={className} />,
    cpu: <Cpu className={className} />,
    film: <Film className={className} />,
    calendar: <Calendar className={className} />,
  };
  return icons[name] || null;
}

// Title Slide
function TitleSlide({ slide }: { slide: typeof slides[0] }) {
  return (
    <div className="relative min-h-screen flex flex-col items-center justify-center px-8">
      {/* Background gradient */}
      <div className="absolute inset-0 bg-gradient-to-b from-amber-500/10 via-transparent to-transparent" />

      <motion.div
        initial={{ opacity: 0, y: 40 }}
        whileInView={{ opacity: 1, y: 0 }}
        viewport={{ once: true }}
        transition={{ duration: 0.8 }}
        className="text-center z-10"
      >
        <p className="text-2xl md:text-3xl text-zinc-400 font-light mb-4 font-title">
          {slide.date}
        </p>
        <h1 className="text-6xl md:text-8xl lg:text-9xl font-bold mb-6">
          <span className="bg-gradient-to-r from-amber-400 via-yellow-300 to-amber-400 bg-clip-text text-transparent">
            {slide.headline}
          </span>
        </h1>
        <p className="text-2xl md:text-4xl text-zinc-300 font-light">
          {slide.subheadline}
        </p>
      </motion.div>

      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        transition={{ delay: 1.5 }}
        className="absolute bottom-12"
      >
        <motion.div animate={{ y: [0, 10, 0] }} transition={{ duration: 2, repeat: Infinity }}>
          <ChevronDown className="w-8 h-8 text-zinc-500" />
        </motion.div>
      </motion.div>
    </div>
  );
}

// Indices Slide
function IndicesSlide({ slide }: { slide: typeof slides[1] }) {
  if (slide.type !== "indices") return null;

  return (
    <div className="min-h-screen flex flex-col items-center justify-center px-8">
      <motion.div
        initial={{ opacity: 0, y: 40 }}
        whileInView={{ opacity: 1, y: 0 }}
        viewport={{ once: true }}
        className="text-center mb-16"
      >
        <h2 className="text-4xl md:text-6xl font-bold text-white mb-4">{slide.title}</h2>
        <p className="text-xl md:text-2xl text-zinc-400">{slide.subtitle}</p>
      </motion.div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-6 md:gap-12 max-w-5xl">
        {slide.data?.map((item, i) => (
          <motion.div
            key={item.ticker}
            initial={{ opacity: 0, y: 30 }}
            whileInView={{ opacity: 1, y: 0 }}
            viewport={{ once: true }}
            transition={{ delay: i * 0.1 }}
            className="text-center"
          >
            <p className="text-lg md:text-xl text-zinc-400 mb-2">{item.ticker}</p>
            <div className="flex items-center justify-center gap-2">
              {item.positive ? (
                <TrendingUp className="w-6 h-6 md:w-8 md:h-8 text-emerald-400" />
              ) : (
                <TrendingDown className="w-6 h-6 md:w-8 md:h-8 text-red-400" />
              )}
              <span className={`text-4xl md:text-6xl font-bold ${item.positive ? "text-emerald-400" : "text-red-400"}`}>
                {item.change}
              </span>
            </div>
          </motion.div>
        ))}
      </div>
    </div>
  );
}

// Highlight Slide (Gold, AI, Hollywood)
function HighlightSlide({ slide }: { slide: typeof slides[2] }) {
  if (slide.type !== "highlight") return null;

  const themeColors = {
    gold: "from-amber-500/20 to-yellow-600/10",
    blue: "from-blue-500/20 to-cyan-600/10",
    red: "from-red-500/20 to-orange-600/10",
  };

  const accentColors = {
    gold: "text-amber-400",
    blue: "text-blue-400",
    red: "text-red-400",
  };

  const theme = (slide.theme as keyof typeof themeColors) || "gold";

  return (
    <div className="min-h-screen flex flex-col items-center justify-center px-8 relative">
      <div className={`absolute inset-0 bg-gradient-to-br ${themeColors[theme]}`} />

      <motion.div
        initial={{ opacity: 0, y: 40 }}
        whileInView={{ opacity: 1, y: 0 }}
        viewport={{ once: true }}
        className="text-center mb-12 z-10"
      >
        {slide.icon && (
          <div className={`inline-flex p-4 rounded-2xl bg-white/5 mb-6 ${accentColors[theme]}`}>
            <IconComponent name={slide.icon} className="w-12 h-12" />
          </div>
        )}
        <h2 className="text-4xl md:text-6xl font-bold text-white mb-4">{slide.title}</h2>
        <p className="text-xl md:text-2xl text-zinc-400">{slide.subtitle}</p>
      </motion.div>

      <div className="flex flex-col md:flex-row gap-8 md:gap-16 z-10">
        {slide.metrics?.map((metric, i) => (
          <motion.div
            key={metric.label}
            initial={{ opacity: 0, scale: 0.9 }}
            whileInView={{ opacity: 1, scale: 1 }}
            viewport={{ once: true }}
            transition={{ delay: i * 0.15 }}
            className="text-center"
          >
            <p className="text-lg text-zinc-400 mb-2">{metric.label}</p>
            <div className="flex items-baseline justify-center gap-1">
              <span className={`text-5xl md:text-7xl font-bold ${accentColors[theme]}`}>
                {metric.value}
              </span>
              <span className="text-xl text-zinc-500">{metric.subtext}</span>
            </div>
            <div className="flex items-center justify-center gap-2 mt-2">
              <ArrowUp className="w-5 h-5 text-emerald-400" />
              <span className="text-2xl font-semibold text-emerald-400">{metric.change}</span>
              <span className="text-sm text-zinc-500">{metric.unit}</span>
            </div>
          </motion.div>
        ))}
      </div>

      {slide.keyPoint && (
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true }}
          transition={{ delay: 0.4 }}
          className="mt-12 p-6 rounded-2xl bg-white/5 border border-white/10 text-center z-10"
        >
          <p className="text-lg text-zinc-400 mb-1">{slide.keyPoint.label}</p>
          <p className={`text-4xl md:text-5xl font-bold ${accentColors[theme]}`}>
            {slide.keyPoint.value}
          </p>
          <p className="text-zinc-500 mt-1">{slide.keyPoint.desc}</p>
        </motion.div>
      )}

      {slide.tickers && (
        <motion.div
          initial={{ opacity: 0 }}
          whileInView={{ opacity: 1 }}
          viewport={{ once: true }}
          transition={{ delay: 0.5 }}
          className="flex gap-3 mt-12 z-10"
        >
          {slide.tickers.map((ticker) => (
            <span key={ticker} className="px-4 py-2 rounded-full bg-white/10 text-sm text-zinc-300">
              {ticker}
            </span>
          ))}
        </motion.div>
      )}
    </div>
  );
}

// Factors Slide
function FactorsSlide({ slide }: { slide: typeof slides[3] }) {
  if (slide.type !== "factors") return null;

  return (
    <div className="min-h-screen flex flex-col items-center justify-center px-8">
      <motion.h2
        initial={{ opacity: 0, y: 40 }}
        whileInView={{ opacity: 1, y: 0 }}
        viewport={{ once: true }}
        className="text-4xl md:text-6xl font-bold text-white mb-16 text-center"
      >
        {slide.title}
      </motion.h2>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-6 max-w-4xl">
        {slide.factors?.map((factor, i) => (
          <motion.div
            key={factor.label}
            initial={{ opacity: 0, x: i % 2 === 0 ? -30 : 30 }}
            whileInView={{ opacity: 1, x: 0 }}
            viewport={{ once: true }}
            transition={{ delay: i * 0.1 }}
            className="flex items-center gap-6 p-6 rounded-2xl bg-white/5 border border-white/10"
          >
            <span className="text-5xl">{factor.icon}</span>
            <div>
              <p className="text-xl font-semibold text-white">{factor.label}</p>
              <p className="text-zinc-400">{factor.desc}</p>
            </div>
          </motion.div>
        ))}
      </div>
    </div>
  );
}

// Single Stat Slide
function SingleStatSlide({ slide }: { slide: typeof slides[5] }) {
  if (slide.type !== "single-stat") return null;

  const themeColors = {
    purple: "from-purple-500/20 to-indigo-600/10",
  };

  return (
    <div className="min-h-screen flex flex-col items-center justify-center px-8 relative">
      <div className={`absolute inset-0 bg-gradient-to-br ${themeColors[slide.theme as keyof typeof themeColors] || ""}`} />

      <motion.div
        initial={{ opacity: 0, scale: 0.8 }}
        whileInView={{ opacity: 1, scale: 1 }}
        viewport={{ once: true }}
        className="text-center z-10"
      >
        <p className="text-2xl text-zinc-400 mb-4">{slide.title}</p>
        <p className="text-8xl md:text-[12rem] font-bold bg-gradient-to-r from-purple-400 to-pink-400 bg-clip-text text-transparent">
          {slide.value}
        </p>
        <p className="text-xl md:text-2xl text-zinc-300 mt-4">{slide.subtitle}</p>
        <p className="text-lg text-zinc-500 mt-2">{slide.note}</p>
      </motion.div>
    </div>
  );
}

// Calendar Slide
function CalendarSlide({ slide }: { slide: typeof slides[7] }) {
  if (slide.type !== "calendar") return null;

  return (
    <div className="min-h-screen flex flex-col items-center justify-center px-8">
      <motion.div
        initial={{ opacity: 0, y: 40 }}
        whileInView={{ opacity: 1, y: 0 }}
        viewport={{ once: true }}
        className="text-center mb-12"
      >
        <div className="inline-flex p-4 rounded-2xl bg-white/5 mb-6 text-blue-400">
          <Calendar className="w-12 h-12" />
        </div>
        <h2 className="text-4xl md:text-6xl font-bold text-white mb-2">{slide.title}</h2>
        <p className="text-xl text-zinc-400">{slide.subtitle}</p>
      </motion.div>

      <div className="space-y-4 max-w-2xl w-full">
        {slide.events?.map((event, i) => (
          <motion.div
            key={event.label}
            initial={{ opacity: 0, x: -30 }}
            whileInView={{ opacity: 1, x: 0 }}
            viewport={{ once: true }}
            transition={{ delay: i * 0.1 }}
            className="flex items-center gap-6 p-6 rounded-2xl bg-white/5 border border-white/10"
          >
            <span className="text-sm font-mono text-blue-400 bg-blue-400/10 px-3 py-1 rounded">
              {event.time}
            </span>
            <div>
              <p className="text-xl font-semibold text-white">{event.label}</p>
              <p className="text-zinc-400">{event.desc}</p>
            </div>
          </motion.div>
        ))}
      </div>
    </div>
  );
}

// Closing Slide
function ClosingSlide({ slide }: { slide: typeof slides[8] }) {
  if (slide.type !== "closing") return null;

  return (
    <div className="min-h-screen flex flex-col items-center justify-center px-8">
      <motion.div
        initial={{ opacity: 0, y: 40 }}
        whileInView={{ opacity: 1, y: 0 }}
        viewport={{ once: true }}
        className="text-center"
      >
        <h2 className="font-title text-6xl md:text-8xl text-white mb-6">
          {slide.headline}
        </h2>
        <p className="text-2xl md:text-3xl text-zinc-400 font-light">
          {slide.tagline}
        </p>
      </motion.div>
    </div>
  );
}

// Render slide based on type
function renderSlide(slide: typeof slides[number], index: number) {
  switch (slide.type) {
    case "title":
      return <TitleSlide key={slide.id} slide={slide} />;
    case "indices":
      return <IndicesSlide key={slide.id} slide={slide as typeof slides[1]} />;
    case "highlight":
      return <HighlightSlide key={slide.id} slide={slide as typeof slides[2]} />;
    case "factors":
      return <FactorsSlide key={slide.id} slide={slide as typeof slides[3]} />;
    case "single-stat":
      return <SingleStatSlide key={slide.id} slide={slide as typeof slides[5]} />;
    case "calendar":
      return <CalendarSlide key={slide.id} slide={slide as typeof slides[7]} />;
    case "closing":
      return <ClosingSlide key={slide.id} slide={slide as typeof slides[8]} />;
    default:
      return null;
  }
}

export default function LandingPage() {
  return (
    <main className="bg-zinc-950">
      {slides.map((slide, index) => (
        <section key={slide.id} className="snap-start">
          {renderSlide(slide, index)}
        </section>
      ))}
    </main>
  );
}
