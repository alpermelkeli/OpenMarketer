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

export type StudioRun = Schemas["StudioRunResponse"];
/** One run as its own route serves it: the run with what it was asked for. */
export type StudioRunDetail = Schemas["StudioRunDetailResponse"];
export type StudioRunRequest = Schemas["StudioRunRequestResponse"];
export type StudioRunList = Schemas["StudioRunListResponse"];
export type StudioRunStatus = Schemas["StudioRunStatus"];
export type AssetRequest = Schemas["AssetRequest"];
export type Platform = Schemas["Platform"];
export type AssetTechnique = Schemas["AssetTechnique"];
export type MediaAsset = Schemas["MediaAssetResponse"];
export type MediaAssetList = Schemas["MediaAssetListResponse"];
export type MediaAssetStatus = Schemas["MediaAssetStatus"];
export type MediaAssetDetail = Schemas["MediaAssetDetailResponse"];
export type MediaAssetVersion = Schemas["MediaAssetVersionResponse"];
/** An image the last analysis found in the project's repository, which the studio may use as a reference. */
export type RepositoryImage = Schemas["RepositoryImageResponse"];
export type RepositoryImageList = Schemas["RepositoryImageListResponse"];
export type RepositoryImageKind = Schemas["RepositoryImageKind"];
export type RepositoryImageSource = Schemas["RepositoryImageSource"];
/** An image a person adds to the project, as its bytes in base64. */
export type RepositoryImageUpload = Schemas["RepositoryImageUpload"];
export type RepositoryImageKindChoice = Schemas["RepositoryImageKindChoice"];

export type WritingRun = Schemas["WritingRunResponse"];
/** One run as its own route serves it: the run with the brief it was given. */
export type WritingRunDetail = Schemas["WritingRunDetailResponse"];
export type WritingRunList = Schemas["WritingRunListResponse"];
export type WritingRunStatus = Schemas["WritingRunStatus"];
export type WritingBrief = Schemas["WritingBrief"];
export type WritingGoal = Schemas["WritingGoal"];
export type WritingPlatform = Schemas["WritingPlatform"];
/** A post a writing run proposed, without the record of how it was written. */
export type ContentItem = Schemas["ContentItemResponse"];
/** A post with the record of how it was written: its plan, alternatives, claims and reports. */
export type ContentItemDetail = Schemas["ContentItemDetailResponse"];
export type ContentItemList = Schemas["ContentItemListResponse"];
export type ContentItemStatus = Schemas["ContentItemStatus"];

/** A task of the orchestrator as a list shows it. */
export type Task = Schemas["TaskResponse"];
/** A task with its request, plan, plan changes, dispatched steps and report. */
export type TaskDetail = Schemas["TaskDetailResponse"];
export type TaskStatus = Schemas["TaskStatus"];
export type TaskOutcome = Schemas["TaskOutcome"];
export type TaskPlan = Schemas["TaskPlan"];
export type TaskScope = Schemas["TaskScope"];
/** One step of a task's plan: a research, studio or writing step with its request. */
export type TaskPlanStep = TaskPlan["steps"][number];
export type ResearchStep = Schemas["ResearchStep"];
export type StudioStep = Schemas["StudioStep"];
export type WritingStep = Schemas["WritingStep"];
export type ResearchRequest = Schemas["ResearchRequest"];
export type TaskAgentName = Schemas["TaskAgentName"];
/** A step the task dispatched: the run it started and how that run ended. */
export type TaskStepRun = Schemas["TaskStepResponse"];
export type TaskStepOutcome = Schemas["TaskStepOutcome"];
export type TaskReport = Schemas["TaskReport"];

export type ProfileVersion = Schemas["ProfileVersionResponse"];
export type ProfileVersionSummary = Schemas["ProfileVersionSummaryResponse"];
export type ProfileVersionList = Schemas["ProfileVersionListResponse"];
export type ProfileStatus = Schemas["ProfileStatus"];
export type ProductProfile = Schemas["ProductProfile"];
export type Product = Schemas["Product"];
export type Feature = Schemas["Feature"];
export type FeatureStatus = Schemas["FeatureStatus"];
export type Brand = Schemas["Brand"];
export type Typeface = Schemas["Typeface"];
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
/** One event a run recorded, as every stream and list answers with it. */
export type RunEvent = Schemas["RunEventResponse"];
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

export type CreativeBudget = Schemas["CreativeBudgetResponse"];
export type CreativeBudgetRequest = Schemas["CreativeBudgetRequest"];
export type CreativeBudgetCap = Schemas["CapResponse"];
export type CapSource = Schemas["CapSource"];
export type MonthlySpend = Schemas["MonthlySpendResponse"];

export type Problem = Schemas["Problem"];
export type ErrorCode = Schemas["ErrorCode"];
export type RequestValidationError = Schemas["HTTPValidationError"];

/** A project's connected accounts, every platform one can be connected on, and whether the API can store a token. */
export type SocialAccounts = Schemas["SocialAccountsResponse"];
/** A project's account on a platform, as the platform described it; never its token. */
export type ConnectedAccount = Schemas["ConnectedAccountResponse"];
export type ConnectablePlatform = Schemas["ConnectablePlatformResponse"];
export type SocialPlatform = Schemas["SocialPlatform"];
export type ConnectionStart = Schemas["ConnectionStartResponse"];
export type ConnectionCallbackResult = Schemas["ConnectionCallbackResponse"];
export type ConnectionOutcome = Schemas["ConnectionOutcome"];
