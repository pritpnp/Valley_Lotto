// Every 10 minutes, ask the store app for the TV numbers.
//
// When the app's copy is more than 5 minutes old, being asked makes it read
// PA's number feed (in the background), so PA is read about every 10 minutes,
// day and night, whether or not a TV is on. GitHub's scheduler was skipping
// most of its 10-minute runs; Netlify's scheduled functions run on time.
//
// Uses the same APP_URL setting as the TV page. Nothing secret is in here.

export default async () => {
  const app = (Netlify.env.get("APP_URL") || "").replace(/\/+$/, "")
  if (!app) {
    console.log("APP_URL isn't set, so there's no app to ask.")
    return
  }
  try {
    const res = await fetch(`${app}/api/numbers`, {
      headers: { "user-agent": "valley-lotto-tv-scheduler" },
      signal: AbortSignal.timeout(25_000),
    })
    console.log(`The app answered ${res.status}.`)
  } catch (e) {
    // Render's free plan puts the app to sleep. This knock still wakes it,
    // even when waking takes longer than the 30 s a scheduled function gets.
    const why = e instanceof Error ? e.name : "error"
    console.log(`The app didn't answer in time (${why}); it's asked again in 10 minutes.`)
  }
}

// Cron runs on UTC; "every 10 minutes" is the same in any time zone.
export const config = { schedule: "*/10 * * * *" }
