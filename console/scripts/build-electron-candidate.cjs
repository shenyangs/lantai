"use strict";

const path = require("node:path");
const { spawnSync } = require("node:child_process");

const rootDir = path.resolve(__dirname, "../..");
const command = path.join(rootDir, "build_and_deploy.sh");
const args = ["--artifact-only"];
const generatedCandidateId = `npm-${new Date()
  .toISOString()
  .replace(/\D/g, "")
  .slice(0, 14)}-${process.pid}`;

const buildEnvironment = {
  ...process.env,
  LANTAI_ALLOW_UNVERIFIED_LOCAL_BUILD:
    process.env.LANTAI_ALLOW_UNVERIFIED_LOCAL_BUILD || "1",
  LANTAI_ALLOW_DIRTY_RELEASE_BUILD:
    process.env.LANTAI_ALLOW_DIRTY_RELEASE_BUILD || "1",
  LANTAI_ALLOW_SAME_VERSION_REBUILD:
    process.env.LANTAI_ALLOW_SAME_VERSION_REBUILD || "1",
  LANTAI_FORCE_ADHOC_LOCAL_BUILD:
    process.env.LANTAI_FORCE_ADHOC_LOCAL_BUILD || "1",
  LANTAI_SKIP_NOTARIZATION: process.env.LANTAI_SKIP_NOTARIZATION || "1",
  LANTAI_DEFER_AGENT_RELIABILITY_SOAK:
    process.env.LANTAI_DEFER_AGENT_RELIABILITY_SOAK || "1",
  LANTAI_RELEASE_CANDIDATE_ID:
    process.env.LANTAI_RELEASE_CANDIDATE_ID || generatedCandidateId,
};

if (process.argv.includes("--print-command")) {
  process.stdout.write(
    `${JSON.stringify(
      {
        command,
        args,
        environment: {
          LANTAI_ALLOW_UNVERIFIED_LOCAL_BUILD:
            buildEnvironment.LANTAI_ALLOW_UNVERIFIED_LOCAL_BUILD,
          LANTAI_ALLOW_DIRTY_RELEASE_BUILD:
            buildEnvironment.LANTAI_ALLOW_DIRTY_RELEASE_BUILD,
          LANTAI_ALLOW_SAME_VERSION_REBUILD:
            buildEnvironment.LANTAI_ALLOW_SAME_VERSION_REBUILD,
          LANTAI_FORCE_ADHOC_LOCAL_BUILD:
            buildEnvironment.LANTAI_FORCE_ADHOC_LOCAL_BUILD,
          LANTAI_SKIP_NOTARIZATION: buildEnvironment.LANTAI_SKIP_NOTARIZATION,
          LANTAI_DEFER_AGENT_RELIABILITY_SOAK:
            buildEnvironment.LANTAI_DEFER_AGENT_RELIABILITY_SOAK,
          LANTAI_RELEASE_CANDIDATE_ID:
            buildEnvironment.LANTAI_RELEASE_CANDIDATE_ID,
        },
      },
      null,
      2,
    )}\n`,
  );
  process.exit(0);
}

if (process.argv.length > 2) {
  process.stderr.write(
    "electron:build accepts configuration through LANTAI_* environment variables only.\n",
  );
  process.exit(2);
}

const completed = spawnSync(command, args, {
  cwd: rootDir,
  env: buildEnvironment,
  stdio: "inherit",
});

if (completed.error) {
  throw completed.error;
}
process.exit(completed.status ?? 1);
