'use client';

import { useMemo, useRef, useEffect, useState } from 'react';
import { motion } from 'framer-motion';
import { Lock, Unlock } from 'lucide-react';
import type { Slide } from '@/types/slide';
import { EPISODE_20251222_SLIDES } from '@/types/slide';
import {
  TitleSlide,
  MarketSummarySlide,
  HeadlineSlide,
  ComparisonSlide,
  StatsSlide,
  TickerIntroSlide,
  TickerAnalysisSlide,
  EventsSlide,
  ClosingSlide,
} from './slides';

interface SlideshowProps {
  currentTurnId: number;
  episodeDate?: string;
}

export function Slideshow({ currentTurnId, episodeDate }: SlideshowProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [lockedSlideIndex, setLockedSlideIndex] = useState<number | null>(null);

  const slides = useMemo(() => {
    if (episodeDate === '20251222') {
      return EPISODE_20251222_SLIDES;
    }
    return EPISODE_20251222_SLIDES;
  }, [episodeDate]);

  const currentSlideIndex = useMemo(() => {
    let slideIndex = 0;
    for (let i = slides.length - 1; i >= 0; i--) {
      if (currentTurnId >= slides[i].turnId) {
        slideIndex = i;
        break;
      }
    }
    return slideIndex;
  }, [currentTurnId, slides]);

  // Auto-scroll to current section (only if not locked)
  useEffect(() => {
    if (lockedSlideIndex !== null) return;

    const targetElement = document.getElementById(`slide-${currentSlideIndex}`);
    if (targetElement) {
      targetElement.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
  }, [currentSlideIndex, lockedSlideIndex]);

  const handleLockToggle = (index: number) => {
    if (lockedSlideIndex === index) {
      // Unlock and scroll to current audio position
      setLockedSlideIndex(null);
    } else {
      // Lock this slide
      setLockedSlideIndex(index);
    }
  };

  function renderSlide(slide: Slide, index: number) {
    const isActive = lockedSlideIndex !== null
      ? index === lockedSlideIndex
      : index === currentSlideIndex;
    const isPast = lockedSlideIndex !== null
      ? index < lockedSlideIndex
      : index < currentSlideIndex;
    const isLocked = lockedSlideIndex === index;

    const content = (() => {
      switch (slide.type) {
        case 'title':
          return <TitleSlide slide={slide} />;
        case 'market-summary':
          return <MarketSummarySlide slide={slide} />;
        case 'headline':
          return <HeadlineSlide slide={slide} />;
        case 'comparison':
          return <ComparisonSlide slide={slide} />;
        case 'stats':
          return <StatsSlide slide={slide} />;
        case 'ticker-intro':
          return <TickerIntroSlide slide={slide} />;
        case 'ticker-analysis':
          return <TickerAnalysisSlide slide={slide} />;
        case 'events':
          return <EventsSlide slide={slide} />;
        case 'closing':
          return <ClosingSlide slide={slide} />;
        default:
          return null;
      }
    })();

    return (
      <motion.section
        id={`slide-${index}`}
        key={slide.id}
        initial={{ opacity: 0, y: 20 }}
        animate={{
          opacity: isActive ? 1 : isPast ? 0.6 : 0.4,
          y: 0,
          scale: isActive ? 1 : 0.98,
        }}
        transition={{ duration: 0.3 }}
        className={`relative transition-all duration-300 ${
          isActive ? 'z-10' : 'z-0'
        }`}
      >
        {isActive && (
          <div className="absolute -left-4 top-0 bottom-0 w-1 bg-black rounded-full" />
        )}

        {/* Lock button */}
        <button
          onClick={() => handleLockToggle(index)}
          className={`absolute top-4 right-4 z-20 p-2 rounded-lg transition-all duration-200 ${
            isLocked
              ? 'bg-black text-white shadow-lg'
              : 'bg-white/80 text-gray-400 hover:bg-gray-100 hover:text-gray-600 border border-gray-200'
          }`}
          title={isLocked ? '슬라이드 고정 해제' : '슬라이드 고정'}
        >
          {isLocked ? <Lock className="w-4 h-4" /> : <Unlock className="w-4 h-4" />}
        </button>

        {content}
      </motion.section>
    );
  }

  const displaySlideIndex = lockedSlideIndex !== null ? lockedSlideIndex : currentSlideIndex;

  return (
    <div
      ref={containerRef}
      className="h-full w-full bg-white overflow-y-auto"
    >
      {/* Progress bar */}
      <div className="sticky top-0 z-20 bg-white/80 backdrop-blur-sm border-b border-gray-100 px-4 py-2">
        {lockedSlideIndex !== null && (
          <div className="flex items-center text-xs text-black font-medium mb-1">
            <Lock className="w-3 h-3 mr-1" />
            고정됨
          </div>
        )}
        <div className="h-1 bg-gray-200 rounded-full overflow-hidden">
          <div
            className="h-full bg-black transition-all duration-300"
            style={{ width: `${((displaySlideIndex + 1) / slides.length) * 100}%` }}
          />
        </div>
      </div>

      {/* Slides as vertical sections */}
      <div className="w-full px-6 py-8 space-y-8">
        {slides.map((slide, index) => renderSlide(slide, index))}
      </div>
    </div>
  );
}
