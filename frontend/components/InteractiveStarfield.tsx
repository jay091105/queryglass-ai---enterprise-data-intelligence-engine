"use client";

import { useEffect, useRef } from "react";

type Star = {
  x: number;
  y: number;
  speed: number;
  heading: number;
  curve: number;
  curvePhase: number;
  curveRate: number;
  radius: number;
  alpha: number;
  color: string;
  useAccent: boolean;
  twinklePhase: number;
  twinkleRate: number;
};

type Link = {
  first: number;
  second: number;
  distance: number;
};

const CONNECTION_DISTANCE = 142;
const MAX_STARS = 180;
const MAX_LINK_DEGREE = 3;
const MAX_POINTER_LINKS = 12;
const STAR_COLORS = ["104,225,239", "70,205,190", "112,169,244", "225,250,255"];

function accentRgb(value: string) {
  const hex = value.trim().match(/^#([\da-f]{3}|[\da-f]{6})$/i);
  if (hex) {
    const digits = hex[1].length === 3
      ? hex[1].split("").map((digit) => digit + digit).join("")
      : hex[1];
    return [0, 2, 4].map((index) => Number.parseInt(digits.slice(index, index + 2), 16)).join(",");
  }

  const rgb = value.match(/rgba?\((\d+)[,\s]+(\d+)[,\s]+(\d+)/i);
  return rgb ? `${rgb[1]},${rgb[2]},${rgb[3]}` : "84,172,191";
}

function createStars(width: number, height: number, isSmallScreen: boolean, accent: string): Star[] {
  const density = isSmallScreen ? 20_000 : 14_000;
  const count = Math.min(isSmallScreen ? 92 : MAX_STARS, Math.max(24, Math.round((width * height) / density)));

  return Array.from({ length: count }, () => {
    const usesAccent = Math.random() < 0.18;
    return {
      x: Math.random() * width,
      y: Math.random() * height,
      speed: 5 + Math.random() * 12,
      heading: Math.random() * Math.PI * 2,
      curve: 0.035 + Math.random() * 0.055,
      curvePhase: Math.random() * Math.PI * 2,
      curveRate: 0.18 + Math.random() * 0.3,
      radius: Math.random() < 0.07 ? 8 + Math.random() * 2 : 3 + Math.random() * 4,
      alpha: 0.5 + Math.random() * 0.45,
      color: usesAccent ? accent : STAR_COLORS[Math.floor(Math.random() * STAR_COLORS.length)],
      useAccent: usesAccent,
      twinklePhase: Math.random() * Math.PI * 2,
      twinkleRate: 0.7 + Math.random() * 1.5,
    };
  });
}

export default function InteractiveStarfield() {
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    const context = canvas?.getContext("2d", { alpha: true });
    if (!canvas || !context) return;

    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
    const isReducedMotion = () => reducedMotion.matches || document.documentElement.dataset.motion === "reduce";
    const stars: Star[] = [];
    const pointer = { x: -1_000, y: -1_000, active: false };
    let interactionStrength = 0;
    let width = 0;
    let height = 0;
    let pixelRatio = 1;
    let frameId = 0;
    let lastFrameTime = 0;
    let elapsedTime = 0;
    let accent = "84,172,191";

    const readAccent = () => {
      accent = accentRgb(getComputedStyle(document.documentElement).getPropertyValue("--accent"));
      for (const star of stars) {
        if (star.useAccent) star.color = accent;
      }
    };

    const resize = () => {
      width = window.innerWidth;
      height = window.innerHeight;
      pixelRatio = Math.min(window.devicePixelRatio || 1, 2);
      canvas.width = Math.round(width * pixelRatio);
      canvas.height = Math.round(height * pixelRatio);
      context.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
      stars.splice(0, stars.length, ...createStars(width, height, width < 700, accent));
      draw(0, false);
    };

    const buildLinks = (cellSize: number) => {
      const grid = new Map<string, number[]>();
      const candidates: Link[] = [];
      const seen = new Set<string>();

      for (let index = 0; index < stars.length; index += 1) {
        const star = stars[index];
        const cellX = Math.floor(star.x / cellSize);
        const cellY = Math.floor(star.y / cellSize);
        const key = `${cellX}:${cellY}`;
        const cell = grid.get(key);
        if (cell) cell.push(index);
        else grid.set(key, [index]);
      }

      const maxDistanceSquared = CONNECTION_DISTANCE * CONNECTION_DISTANCE;
      for (let index = 0; index < stars.length; index += 1) {
        const star = stars[index];
        const cellX = Math.floor(star.x / cellSize);
        const cellY = Math.floor(star.y / cellSize);
        const nearest: Link[] = [];
        for (let offsetX = -1; offsetX <= 1; offsetX += 1) {
          for (let offsetY = -1; offsetY <= 1; offsetY += 1) {
            const neighbors = grid.get(`${cellX + offsetX}:${cellY + offsetY}`);
            if (!neighbors) continue;
            for (const neighborIndex of neighbors) {
              if (neighborIndex <= index) continue;
              const neighbor = stars[neighborIndex];
              const dx = star.x - neighbor.x;
              const dy = star.y - neighbor.y;
              const distanceSquared = dx * dx + dy * dy;
              if (distanceSquared > maxDistanceSquared) continue;
              nearest.push({ first: index, second: neighborIndex, distance: Math.sqrt(distanceSquared) });
            }
          }
        }

        nearest.sort((first, second) => first.distance - second.distance);
        for (const link of nearest.slice(0, 4)) {
          const key = `${link.first}:${link.second}`;
          if (!seen.has(key)) {
            seen.add(key);
            candidates.push(link);
          }
        }
      }

      candidates.sort((first, second) => first.distance - second.distance);
      const degree = new Uint8Array(stars.length);
      const links: Link[] = [];
      for (const link of candidates) {
        if (degree[link.first] >= MAX_LINK_DEGREE || degree[link.second] >= MAX_LINK_DEGREE) continue;
        degree[link.first] += 1;
        degree[link.second] += 1;
        links.push(link);
      }
      return links;
    };

    const draw = (elapsed: number, animate: boolean, timestamp = elapsedTime) => {
      context.clearRect(0, 0, width, height);
      if (animate && elapsed > 0) {
        const targetStrength = pointer.active ? 1 : 0;
        const responseTime = pointer.active ? 110 : 420;
        const blend = 1 - Math.exp(-elapsed / responseTime);
        interactionStrength += (targetStrength - interactionStrength) * blend;
        elapsedTime += elapsed / 1000;

        const delta = Math.min(elapsed / 1000, 0.04);
        const margin = 14;
        for (const star of stars) {
          star.heading += Math.sin(elapsedTime * star.curveRate + star.curvePhase) * star.curve * delta;
          star.x += Math.cos(star.heading) * star.speed * delta;
          star.y += Math.sin(star.heading) * star.speed * delta;
          if (star.x < -margin) star.x = width + margin;
          else if (star.x > width + margin) star.x = -margin;
          if (star.y < -margin) star.y = height + margin;
          else if (star.y > height + margin) star.y = -margin;
        }
      }

      const reduced = isReducedMotion();
      const activeStrength = reduced ? 0 : interactionStrength;
      const pointerRadius = width < 700 ? 128 : 190;
      const pointerRadiusSquared = pointerRadius * pointerRadius;
      const nearbyStars: Array<{ index: number; distanceSquared: number }> = [];
      for (let index = 0; index < stars.length; index += 1) {
        const star = stars[index];
        const dx = star.x - pointer.x;
        const dy = star.y - pointer.y;
        const distanceSquared = dx * dx + dy * dy;
        if (activeStrength > 0 && distanceSquared < pointerRadiusSquared) {
          nearbyStars.push({ index, distanceSquared });
        }

        const proximity = activeStrength > 0 && distanceSquared < pointerRadiusSquared
          ? 1 - Math.sqrt(distanceSquared) / pointerRadius
          : 0;
        const twinkle = reduced ? 1 : 0.82 + Math.sin(timestamp / 1000 * star.twinkleRate + star.twinklePhase) * 0.18;
        const glowAlpha = Math.min(0.26, star.alpha * (0.18 + proximity * activeStrength * 0.16) * twinkle);
        const radius = star.radius * (1 + proximity * activeStrength * 0.12);

        context.globalAlpha = glowAlpha;
        context.fillStyle = `rgb(${star.color})`;
        context.beginPath();
        context.arc(star.x, star.y, radius * 2.8, 0, Math.PI * 2);
        context.fill();
        context.globalAlpha = Math.min(1, star.alpha * twinkle + proximity * activeStrength * 0.2);
        context.shadowBlur = radius * (1.3 + proximity * activeStrength);
        context.shadowColor = `rgba(${star.color},0.72)`;
        context.beginPath();
        context.arc(star.x, star.y, radius, 0, Math.PI * 2);
        context.fill();
        context.shadowBlur = 0;
      }
      context.globalAlpha = 1;

      if (width < 1 || height < 1) return;
      const links = buildLinks(CONNECTION_DISTANCE);
      const distanceFade = Math.max(1, CONNECTION_DISTANCE);
      context.lineWidth = width < 700 ? 1 : 1.3;
      for (let linkIndex = 0; linkIndex < links.length; linkIndex += 1) {
        const link = links[linkIndex];
        const first = stars[link.first];
        const second = stars[link.second];
        const middleX = (first.x + second.x) / 2;
        const middleY = (first.y + second.y) / 2;
        const pointerDistance = Math.hypot(middleX - pointer.x, middleY - pointer.y);
        const proximity = activeStrength > 0 ? Math.max(0, 1 - pointerDistance / pointerRadius) * activeStrength : 0;
        const distanceStrength = 1 - link.distance / distanceFade;
        const alpha = 0.14 + distanceStrength * 0.2 + proximity * 0.12;

        context.strokeStyle = `rgba(${accent},${alpha})`;
        if (distanceStrength > 0.42) {
          context.shadowBlur = 5 + distanceStrength * 4;
          context.shadowColor = `rgba(${accent},${Math.min(0.28, alpha * 0.72)})`;
        }
        context.beginPath();
        context.moveTo(first.x, first.y);
        context.lineTo(second.x, second.y);
        context.stroke();
        context.shadowBlur = 0;

        if (!reduced && linkIndex % 5 === 0) {
          const phase = ((timestamp / 5200) + link.first * 0.173 + link.second * 0.071) % 1;
          const pulseX = first.x + (second.x - first.x) * phase;
          const pulseY = first.y + (second.y - first.y) * phase;
          context.globalAlpha = 0.76 + proximity * 0.2;
          context.fillStyle = `rgb(${first.color})`;
          context.shadowBlur = 11;
          context.shadowColor = `rgba(${first.color},0.95)`;
          context.beginPath();
          context.arc(pulseX, pulseY, 2.2, 0, Math.PI * 2);
          context.fill();
          context.shadowBlur = 0;
          context.globalAlpha = 1;
        }
      }

      if (activeStrength < 0.01 || reduced) return;
      nearbyStars.sort((first, second) => first.distanceSquared - second.distanceSquared);
      context.lineWidth = 0.7;
      for (const { index } of nearbyStars.slice(0, MAX_POINTER_LINKS)) {
        const star = stars[index];
        const dx = star.x - pointer.x;
        const dy = star.y - pointer.y;
        const proximity = 1 - Math.sqrt(dx * dx + dy * dy) / pointerRadius;
        context.strokeStyle = `rgba(${accent},${proximity * activeStrength * 0.16})`;
        context.beginPath();
        context.moveTo(pointer.x, pointer.y);
        context.lineTo(star.x, star.y);
        context.stroke();
      }
    };

    const animate = (time: number) => {
      if (document.visibilityState !== "visible" || isReducedMotion()) {
        frameId = 0;
        return;
      }
      const elapsed = lastFrameTime ? time - lastFrameTime : 0;
      lastFrameTime = time;
      draw(elapsed, true, time);
      frameId = window.requestAnimationFrame(animate);
    };

    const startAnimation = () => {
      if (frameId || document.visibilityState !== "visible" || isReducedMotion()) return;
      lastFrameTime = 0;
      frameId = window.requestAnimationFrame(animate);
    };

    const stopAnimation = () => {
      if (frameId) window.cancelAnimationFrame(frameId);
      frameId = 0;
      lastFrameTime = 0;
    };

    const handlePointerMove = (event: PointerEvent) => {
      if (event.pointerType === "touch") return;
      pointer.x = event.clientX;
      pointer.y = event.clientY;
      pointer.active = true;
    };

    const handlePointerLeave = () => {
      pointer.active = false;
    };

    const handleVisibilityChange = () => {
      if (document.visibilityState === "visible") startAnimation();
      else stopAnimation();
    };

    const handleMotionChange = () => {
      if (isReducedMotion()) {
        stopAnimation();
        draw(0, false);
      } else {
        startAnimation();
      }
    };

    const themeObserver = new MutationObserver(() => {
      readAccent();
      draw(0, false);
      if (isReducedMotion()) stopAnimation();
      else startAnimation();
    });

    readAccent();
    resize();
    themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme", "data-motion"] });
    window.addEventListener("resize", resize, { passive: true });
    window.addEventListener("pointermove", handlePointerMove, { passive: true });
    window.addEventListener("pointerleave", handlePointerLeave, { passive: true });
    document.addEventListener("visibilitychange", handleVisibilityChange);
    reducedMotion.addEventListener("change", handleMotionChange);
    startAnimation();

    return () => {
      stopAnimation();
      themeObserver.disconnect();
      window.removeEventListener("resize", resize);
      window.removeEventListener("pointermove", handlePointerMove);
      window.removeEventListener("pointerleave", handlePointerLeave);
      document.removeEventListener("visibilitychange", handleVisibilityChange);
      reducedMotion.removeEventListener("change", handleMotionChange);
    };
  }, []);

  return <canvas ref={canvasRef} className="interactive-starfield" aria-hidden="true" />;
}
