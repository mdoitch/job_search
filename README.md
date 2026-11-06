# Maya's job board

Every morning this pulls new data analyst, business analyst, data scientist and project manager openings in California, scores each one against your resume, and shows them on a website.

## Setup (about 20 minutes, all free)

1. **Get Adzuna API keys.** Sign up at https://developer.adzuna.com and copy your App ID and App Key.
2. **Make a GitHub repo.** Create a new repository and upload everything in this folder (keep the `.github/workflows` folder structure).
3. **Add your keys as secrets.** In the repo, go to Settings > Secrets and variables > Actions, and add `ADZUNA_APP_ID` and `ADZUNA_APP_KEY`.
4. **Turn on the website.** Settings > Pages, set Source to "Deploy from a branch", branch `main`, folder `/docs`.
5. **Run it once.** Go to the Actions tab, open "Fetch jobs daily", and click "Run workflow". After that it runs on its own every morning.

Your site will be at `https://<your-username>.github.io/<repo-name>/`.

## Changing what it looks for

Everything lives in `config.json`:

- `roles`: the job titles that count for each role type.
- `target_companies`: companies that get the "On your list" tag and a fit boost.
- `company_feeds`: extra companies to pull straight from their careers page. Works for any company whose careers page runs on Greenhouse, Lever or Ashby. The board name is in their job page URL, e.g. `jobs.lever.co/<board>` or `boards.greenhouse.io/<board>`.
- `profile.skills`: the skills from your resume used for the fit score. Add new ones as you learn them (Tableau, dbt, etc.).
- `include_remote_us`: set to `false` for California-only.

## How the fit score works (0 to 100)

- Up to 48 points for skills from your resume that the posting mentions.
- +25 if it reads as early career (new grad, entry level, intern, associate, 0–2 years).
- +10 if it mentions statistics, business analytics or a master's.
- +17 if it's at a company on your list.
- -35 for senior, staff or lead titles, and -20 if it asks for 5+ years.

Adzuna only shares a short snippet of each posting, so fit scores for those jobs are rougher than for jobs pulled straight from company sites.

## Privacy

On a free GitHub account, Pages sites need a public repo, so anyone with the link could see the job list. Your resume isn't in the repo, only the skill keywords in `config.json`.
