/**
 * Short names for the API's schemas. Every type here is an alias of the generated
 * contract (`schema.d.ts`, written by `pnpm api:types`); none is written by hand.
 */

import type { components } from "./schema";

type Schemas = components["schemas"];

export type Project = Schemas["ProjectResponse"];
export type ProjectList = Schemas["ProjectListResponse"];
export type CreateProjectRequest = Schemas["CreateProjectRequest"];

export type AnalysisRun = Schemas["AnalysisRunResponse"];
export type AnalysisRunList = Schemas["AnalysisRunListResponse"];
export type AnalysisRunStatus = Schemas["AnalysisRunStatus"];

export type ProfileVersion = Schemas["ProfileVersionResponse"];
export type ProfileVersionSummary = Schemas["ProfileVersionSummaryResponse"];
export type ProfileVersionList = Schemas["ProfileVersionListResponse"];
export type ProfileStatus = Schemas["ProfileStatus"];
export type ProductProfile = Schemas["ProductProfile"];
export type Product = Schemas["Product"];
export type Feature = Schemas["Feature"];
export type FeatureStatus = Schemas["FeatureStatus"];
export type Brand = Schemas["Brand"];
export type Audience = Schemas["Audience"];
export type BusinessModel = Schemas["BusinessModel"];
export type BusinessModelType = Schemas["BusinessModelType"];
export type Measurement = Schemas["Measurement"];
export type Evidence = Schemas["Evidence"];

export type Problem = Schemas["Problem"];
export type ErrorCode = Schemas["ErrorCode"];
export type RequestValidationError = Schemas["HTTPValidationError"];
