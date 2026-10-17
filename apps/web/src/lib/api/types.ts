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

export type ResearchRun = Schemas["ResearchRunResponse"];
export type ResearchRunList = Schemas["ResearchRunListResponse"];
export type ResearchRunStatus = Schemas["ResearchRunStatus"];
export type ResearchReport = Schemas["ResearchReportResponse"];

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

export type Proposal = Schemas["ProposalResponse"];
export type ProposalList = Schemas["ProposalListResponse"];
export type ProposalStatus = Schemas["ProposalStatus"];
export type ActionEffect = Schemas["ActionEffect"];

export type Chat = Schemas["ChatThreadResponse"];
export type ChatList = Schemas["ChatThreadListResponse"];
export type ChatMessage = Schemas["ChatMessageResponse"];
export type ChatMessageList = Schemas["ChatMessageListResponse"];
export type ChatTurn = Schemas["ChatTurnResponse"];
export type ChatEvent = Schemas["ChatEventResponse"];
export type ChatEventList = Schemas["ChatEventListResponse"];

export type CredentialStatus = Schemas["CredentialStatusResponse"];
export type CredentialList = Schemas["CredentialListResponse"];
export type CredentialSource = Schemas["CredentialSource"];

export type ModelSettings = Schemas["ModelSettingsResponse"];
export type TierSetting = Schemas["TierSettingResponse"];
export type TierModelSource = Schemas["TierModelSource"];
export type RoleSetting = Schemas["RoleSettingResponse"];
export type RoleModelSource = Schemas["RoleModelSource"];
export type RoleChoiceRequest = Schemas["RoleChoiceRequest"];
export type ResolvedModel = Schemas["ResolvedModelResponse"];
export type RejectedParameters = Schemas["RejectedParametersResponse"];
export type RejectableParameter = RejectedParameters["chosen"][number];
export type ChoiceBounds = Schemas["ChoiceBoundsResponse"];
export type UnreadableChoice = Schemas["UnreadableChoiceResponse"];
export type ModelCatalog = Schemas["ModelCatalogResponse"];
export type CatalogModel = Schemas["CatalogModelResponse"];
export type ModelCheck = Schemas["ModelCheckResponse"];
export type ModelProblem = Schemas["ModelProblemResponse"];
export type CapabilityProblem = Schemas["CapabilityProblem"];

export type AnalysisSettings = Schemas["AnalysisSettingsResponse"];
export type AnalysisLimitsRequest = Schemas["AnalysisLimitsRequest"];

export type Problem = Schemas["Problem"];
export type ErrorCode = Schemas["ErrorCode"];
export type RequestValidationError = Schemas["HTTPValidationError"];
