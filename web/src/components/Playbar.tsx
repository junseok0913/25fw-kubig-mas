'use client';

import { useRef, useState, useEffect } from 'react';

interface PlaybarProps {
  audioSrc: string;
  currentTime: number;
  onTimeUpdate: (time: number) => void;
}

function formatTime(seconds: number): string {
  const mins = Math.floor(seconds / 60);
  const secs = Math.floor(seconds % 60);
  return `${mins}:${secs.toString().padStart(2, '0')}`;
}

export default function Playbar({ audioSrc, currentTime, onTimeUpdate }: PlaybarProps) {
  const audioRef = useRef<HTMLAudioElement>(null);
  const [isPlaying, setIsPlaying] = useState(false);
  const [duration, setDuration] = useState(0);
  const [internalTime, setInternalTime] = useState(0);
  const [playbackRate, setPlaybackRate] = useState(1);
  const [isLooping, setIsLooping] = useState(false);

  // Sync external currentTime with audio
  useEffect(() => {
    if (audioRef.current && Math.abs(audioRef.current.currentTime - currentTime) > 0.5) {
      audioRef.current.currentTime = currentTime;
    }
  }, [currentTime]);

  const handleTimeUpdate = () => {
    if (audioRef.current) {
      setInternalTime(audioRef.current.currentTime);
      onTimeUpdate(audioRef.current.currentTime);
    }
  };

  const handleLoadedMetadata = () => {
    if (audioRef.current) {
      setDuration(audioRef.current.duration);
    }
  };

  const togglePlay = () => {
    if (audioRef.current) {
      if (isPlaying) {
        audioRef.current.pause();
      } else {
        audioRef.current.play();
      }
      setIsPlaying(!isPlaying);
    }
  };

  const handleSeek = (e: React.ChangeEvent<HTMLInputElement>) => {
    const time = parseFloat(e.target.value);
    if (audioRef.current) {
      audioRef.current.currentTime = time;
      setInternalTime(time);
      onTimeUpdate(time);
    }
  };

  const skipBackward = () => {
    if (audioRef.current) {
      audioRef.current.currentTime = Math.max(0, audioRef.current.currentTime - 10);
    }
  };

  const skipForward = () => {
    if (audioRef.current) {
      audioRef.current.currentTime = Math.min(duration, audioRef.current.currentTime + 10);
    }
  };

  const cyclePlaybackRate = () => {
    const rates = [1, 1.25, 1.5, 1.75, 2];
    const currentIndex = rates.indexOf(playbackRate);
    const nextIndex = (currentIndex + 1) % rates.length;
    const newRate = rates[nextIndex];
    setPlaybackRate(newRate);
    if (audioRef.current) {
      audioRef.current.playbackRate = newRate;
    }
  };

  const toggleLoop = () => {
    setIsLooping(!isLooping);
    if (audioRef.current) {
      audioRef.current.loop = !isLooping;
    }
  };

  const progress = duration > 0 ? (internalTime / duration) * 100 : 0;

  return (
    <div className="bg-bg-primary h-[80px] w-full flex flex-col items-center justify-center gap-2">
      <audio
        ref={audioRef}
        src={audioSrc}
        onTimeUpdate={handleTimeUpdate}
        onLoadedMetadata={handleLoadedMetadata}
        onEnded={() => setIsPlaying(false)}
      />

      {/* Controls */}
      <div className="flex items-center gap-4">
        {/* Playback speed */}
        <button
          onClick={cyclePlaybackRate}
          className="w-8 h-8 flex items-center justify-center"
          title={`Speed: ${playbackRate}x`}
        >
          <img src="/icons/gauge.svg" alt="speed" className="w-4 h-4 opacity-70" />
        </button>

        {/* Skip backward */}
        <button
          onClick={skipBackward}
          className="w-8 h-8 flex items-center justify-center"
        >
          <img src="/icons/skip-back.svg" alt="skip back" className="w-4 h-4" />
        </button>

        {/* Play/Pause */}
        <button
          onClick={togglePlay}
          className="w-8 h-8 bg-black rounded-full flex items-center justify-center"
        >
          <img
            src={isPlaying ? '/icons/pause.svg' : '/icons/play.svg'}
            alt={isPlaying ? 'pause' : 'play'}
            className="w-4 h-4"
          />
        </button>

        {/* Skip forward */}
        <button
          onClick={skipForward}
          className="w-8 h-8 flex items-center justify-center"
        >
          <img src="/icons/skip-forward.svg" alt="skip forward" className="w-4 h-4" />
        </button>

        {/* Loop toggle */}
        <button
          onClick={toggleLoop}
          className={`w-8 h-8 flex items-center justify-center ${isLooping ? 'opacity-100' : 'opacity-50'}`}
        >
          <img src="/icons/repeat.svg" alt="repeat" className="w-4 h-4" />
        </button>
      </div>

      {/* Seekbar */}
      <div className="flex items-center gap-2 w-[538px]">
        <span className="font-bold text-[12px] text-[rgba(0,0,0,0.7)] w-[26px] text-right">
          {formatTime(internalTime)}
        </span>
        <div className="relative flex-1 h-3">
          <div className="absolute top-1/2 -translate-y-1/2 left-0 right-0 h-1 bg-[rgba(0,0,0,0.3)] rounded-[2px]" />
          <div
            className="absolute top-1/2 -translate-y-1/2 left-0 h-1 bg-[rgba(0,0,0,0.7)] rounded-[2px]"
            style={{ width: `${progress}%` }}
          />
          <input
            type="range"
            min={0}
            max={duration || 100}
            value={internalTime}
            onChange={handleSeek}
            className="absolute top-0 left-0 w-full h-full opacity-0 cursor-pointer"
          />
        </div>
        <span className="font-bold text-[12px] text-[rgba(0,0,0,0.7)] w-[26px]">
          {formatTime(duration)}
        </span>
      </div>
    </div>
  );
}
