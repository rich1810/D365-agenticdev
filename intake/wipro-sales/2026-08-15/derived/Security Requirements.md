**Functional Requirements Document**

Security Model

**Wipro Technologies**

**Prepared By**:

|  |  |
| --- | --- |
| Microsoft Services |  |
| 06/12/2007 |  |

Version: 1.1

**Revision & Sign-off Sheet**

Change Record

| Date | Author | Version | Change Reference |
| --- | --- | --- | --- |
|  |  |  |  |
|  |  |  |  |
|  |  |  |  |
|  |  |  |  |
|  |  |  |  |
|  |  |  |  |
|  |  |  |  |

Approvers

| Name | Version Approved | Position | Date |
| --- | --- | --- | --- |
|  |  |  |  |
|  |  |  |  |
|  |  |  |  |
|  |  |  |  |

Distribution

| Name | Position |
| --- | --- |
| Ashish S | PM |
| Richa Jain | Consultant |
|  |  |
|  |  |

Document Properties

| Item | Details |
| --- | --- |
| Document Title | Functional Specification |
| Author | Richa Jain |
| Creation Date | December 06, 2007 |
| Last Updated | December 11, 2007 |

1. **Introduction**

Wipro Technologies desires to implement Microsoft CRM to manage their Sales & Marketing processes across different geographies. MS CRM 4.0 will be the core CRM system used by Wipro. The functional requirements document is prepared by discussing and understanding the processes of Wipro from their key users.

This document details the functional requirements for the security process as followed in Wipro.

* 1. ***Reference***

This document has been prepared after the security requirement discussions with Wipro team.

* 1. ***Abbreviations/Glossary***

|  |  |
| --- | --- |
| **Abbreviation** | **Description** |
| BU | Business Unit |
| Geography | Represents a country/continent – US/Europe/APAC/Japan |
| Global Account | An account spanning geographies |
| Regions | Represents a smaller geographical unit within the country – e.g. US East |
| Practice | Represents a sub vertical within a vertical. For e.g. Within Retail & CPG vertical, Grocery retailing will be a practice |
| CRM BU | A term specific to CRM security model |

1. **Business Requirements**
   1. ***Overview***

Wipro has various users in different job functions that require different access on different data. Data represents Accounts, Contacts, Opportunities, Leads and Activities. Wipro has functions spanning geographies and the organization hierarchy has vertical and horizontal elements having specific requirements of data access.

Following is a list of organizations as well as roles identified in Wipro:

1. **Sales Organization Roles**
2. BDM – Can see his/her own accounts/opportunities for a region within a vertical (multiple esp. in Europe and Japan)
3. RM – Can see all the data for a region within a vertical (multiple esp. in Europe and Japan)
4. RGM – Can see all the data for all regions within a vertical (multiple. Esp. in case of Europe and Japan)
5. RVP – Can see all the data for a BU
6. Geo Head – Can see all the data within a geography
7. Client Engagement Manager (CEM) – owns a global account (across Geo’s)
8. SBDM – Can see all accounts and the mapped contacts (view only) in the region for all verticals. Also only those opportunities that are tagged to him/her will be visible to the SBDM
9. SRM - Can see all accounts and the mapped contacts (view only) in the region for all verticals. Also only those opportunities that are tagged to his/her reportee opportunities are visible to the SRM
10. SRVP - Can see all accounts and mapped contacts (view only) in the Geo for all verticals. Also only those opportunities that are tagged to his/her reportee opportunities and contacts tagged to his/her service at Geo Level.
11. Central Marketing Manager will have data access for all verticals, service lines across geographies. The Central Marketing manager will have global read access on the following data:
    1. Leads
    2. Accounts
    3. Contacts
12. Central Marketing Manage will be able to create leads and contacts and update the leads and contacts owned by him. The user in this role cannot convert lead to an account.
13. **Presales**
14. Vertical Pre Sales Head (PSH)
15. Demand Generation Exec
16. **Marketing**
17. Vertical Marketing manager
18. Central Marketing manager
19. **Delivery Organization Roles ( Vertical)**
20. Vertical Delivery Head
21. Vertical Practice Head
22. Delivery Manager
23. Program Manager (usually onsite)
24. **Delivery Organization Roles (Service Lines)**
25. Service Delivery Head
26. Service Practice Head
27. Service Delivery Manager
28. Program Manager (usually Onsite)
    1. ***Business Requirements***
       1. ***Sales organization access requirements:***

* Sales Organization Members have the following access requirements based on the different job functions:
  + All BDMs for a region (sometime multiple regions) within a vertical (multiple verticals in case of Europe/Japan) will be able to see only the data that they own within that region. E.g., BDM in US East Region for Retail vertical can only see the data he owns. He will not able to see data of any other BDM in the same vertical and same region until explicitly shared with him.
  + A BDM owns an account and any opportunities/contacts/activities etc within that account
  + All RMs for a region (sometime multiple regions) within a vertical (sometime multiple regions) will be able to see all the data within that region and vertical. For e.g. The RMs for Retail Vertical within US East will be able to see all the accounts/opportunities for retail within the US East region however they will be unable to see the data for retail in US west. They will be unable to see any data outside the scope of the vertical and region boundaries.
  + All the RGMs at the vertical level (Multiple in case on Europe/Japan) will be able to see all the data pertaining to that vertical within all regions. For e.g. The RGMs for Retail Vertical will be able to see all the accounts/opportunities for retail within US.
  + The RVP at Business Unit (BU) level will be able to see all the data within that BU. For e.g. the RVP of Enterprise business unit in US can see all the data within Enterprise.
  + Geo head will be able to see all the data within that geography. For e.g. the geo head of US can see all the data in US geography.
  + Client Engagement Manager (CEM) takes care of Global accounts and should be able to see Global Accounts he owns and all the sub accounts and opportunities mapped to the sub accounts across geographies. E.g. CEM for Shell account although belongs to Europe Sales organization can view the data for the sub accounts in the Shell account across different geographies. Client Manager reports to RVP and in the case of global accounts, RVP should be able to get the same access on the global account and its sub accounts with mapped opportunities.
    1. ***Service Lines access requirements:***
* Service Lines members work in a region across all verticals. For e.g. SBDMs in US East should be able to see all accounts (read only) for all verticals within US East. Service line BDM should also be able to see all the opportunities that they have created or that are tagged to them (tagging done by Vertical BDM) and the contacts on the accounts.
  + The information on who created the opportunity should also be available on the opportunity view.
* They should also be able to create opportunities that will get auto assigned to BDM who owns the parent account of that opportunity. The SBDM should continue to have access on the opportunity created by them.
* Any accounts/opportunities that an SBDM has access on should be automatically visible to the SRM who is the manager of SBDM.
* SRVP should be able to see all the data that is available to SRM reporting to the SRVP at Geo level
  + 1. ***Delivery organization access requirements:***
* Delivery Teams work within a vertical spanning across different geographies.
* Delivery managers should be able to see (view access only) accounts/contacts/opportunities for a practice for different geographies tagged to them. Vertical BDM will be responsible for team tagging where he will provide access to the Delivery Manager.
* Practice Head will be able to see (view access only) accounts/opportunities for a practice for different geographies. E.g., Practice Head for offshore Food and Beverage (F&B) practice can view all F&B opportunities across different geographies.
* Delivery head will see all data belonging to his reportees.
* Program Manager will have the same level of access as the delivery manager. Both program managers and delivery managers will be given manual access on accounts/contacts/opportunities as and when needed
  + 1. ***Presales access requirements:***
* Vertical & Service Pre Sales head will be able to see accounts/contacts/opportunities for a vertical/Service across all geographies.
  + 1. ***Marketing access requirements:***
* Central Marketing will be able to see Leads, Accounts & Contacts (read only) for all vertical and all geographies
* Vertical Marketing Manager will be able to see Leads, Accounts & Contacts (read only) for a vertical across all geographies.
* For Leads generated by Marketing, they should be able to see opportunities
  1. ***Security Structure within Microsoft Dynamics CRM 4.0***

**For US Organization:**

Wipro

US

Europe

APAC

Enterprise

Telecom

BFS

Retail

Manufacturing

US East

US West

US Central

**Hierarchy structure representation in CRM 4.0 (Partial Structure of Wipro US Org)**

CRM Security is a combination of Business Units and Security Roles. A user is associated with one Business Unit and can be added to one or more security roles.

The Geographies, Regions, BUs and Verticals will be set up as CRM business units (represented by double lined blue colored rectangles) in the hierarchy shown above. All the job titles will be created as security roles (represented by dashed ovals). The roles will be defined with specific privileges on each entity (Account, Opportunities, Contact and Lead are individual entities).

* + 1. ***Access to Sales Organization***

Sales organization members will be added to the role specific to their job title. Following are the access and privileges defined for Sales Org Members:

**BDM**: User level access

**RM**: Business Unit (CRM BU) level access

**RGM, RVP, Geo Head**: Parent Child Access

Within Sales Organization, there is a special role of Client Engagement Manager that owns a global account. This user creates global accounts that can have sub accounts in different geographies. Any opportunities identified against these sub accounts will be visible to the CEM although owned by the BDMs. This user will have user level access in the system. On creation of a sub account within a global account, the sub account will be automatically shared with the CEM on the global account. The opportunities mapped to these sub accounts will also be shared automatically.

* + 1. ***Access for Service Line Teams:***

Service Line will be created as a role with all service line members having user level access on Account and Opportunities.

**Account Access**

Accounts of a region will be shared with an SBDM based on the region of the SBDM. The managers in the reporting hierarchy will also get the automatic access. The access on account will be limited to some basic minimal information on Account.

**Opportunity Access**

SBDM will have the access to create an opportunity mapped to an existing account. On creation of an opportunity, the BDM of the parent account will be given automatic access and the SBDM will continue to have write access on this opportunity. The BDM will be sent a notification for the same.

* + 1. ***Access for Delivery Organization Teams:***

Each practice will be defined as a team in CRM 4.0. An administrator will have to do the following:

* At the time of creating a user from delivery organization, specify the vertical that user belongs to.
* Add the user to the predefined team based on that vertical name.
* If the user changes from one vertical to another, the administrator will take the following steps:
  + Update the user record
  + Remove the user from the existing team
  + Add him to the new team corresponding to his practice.

**Account/Opportunity Access**

At the account/opportunity creation level, vertical and practice will be identified and based on the practice, the account/opportunity will be automatically shared with that team.

***Assumptions:***

* Delivery Manager will be given manual access by BDM as and when required. Delivery Manager will be added as a CRM user with user level permissions so that he/she can view only that data that is shared with him.
* At the time of creating a user in CRM, the administrator will input the following information:
  + Manager
  + Region (s) of that user
  + Vertical (s) (for Delivery org user)
  + Practice (for Delivery org user)
  + Service Line
  + Role of the user i.e. Service, Delivery or Sales
* The global accounts will not have any associated opportunities. Opportunities will be associated only with sub accounts within the global account.

**For Non US Organization:**

Wipro

Non \_US

US

Enterprise

Telecom

BFS

Retail1

Dummy BU

Retail2

TMTS

HLS

Consolidated Region in France

UK

France

**Hierarchy structure representation in CRM 4.0 (Partial Structure of Wipro non USOrg)**

* + 1. ***Access within Europe/Japan/APAC geographies***

In the organization structure in these geographies, following scenarios might occur:

* BDM/RM usually functions across multiple regions. – For e.g. BDM belonging to UK should have the flexibility of creating Accounts in France. At any point in time, a BDM can belong to only one CRM Business Unit. To meet the requirement of a BDM having the ability to create data across various regions, the following will be provided:
  + In the security structure, a consolidated region can be created
  + On the account/opportunity user interface, user will be able to specify the region for that account/opportunity.
  + This will enable the users to create data across regions and can be utilized for region-wise reporting purpose.
* One BU might be divided into more than one (as part of organizational realignment) and the verticals within the original BU are shifted across these BU. For e.g. Enterprise might be realigned as Enterprise and Enterprise Manufacturing. In this case, retail continue to lie within Enterprise and Manufacturing is realigned to Enterprise Manufacturing
  + One new CRM BU can be created for Enterprise Manufacturing.
  + Administrator can then change the parent business unit for Manufacturing to Enterprise Manufacturing. This can be easily accomplished from the CRM Business Unit Administration screen.
  + All the data will accordingly be realigned
* One RGM might be managing two verticals – An RGM might be taking care of Retail and manufacturing at the same time within UK.
  + A dummy CRM business unit can be created with the two verticals as child BU.
  + An RGM can then be associated to dummy business unit with the parent child access.

This structure requires creation of consolidated CRM business units to meet the above mentioned requirements.

* 1. ***Open Items***
* Detailed level of security at entity level
* Delivery Organization based on Service Lines
* Access to DG Team
* Access to Alliance Manager
* Delivery Managers access to create Opportunities.
* Access to BFM
* Security Solution Implementation: has to be decided in terms of multiple BU structure or auto sharing for every record. This decision will be taken by Wipro team once the overhead in both the approaches has been identified. The current document refers to multiple BU design; any change from that will require another level of impact analysis and documentation.
* Service Line Delivery Roles Access
  1. ***Closed Items***
* Different Role for RGM and RVP
* Ability to create data in BU other than the user’s own BU
* SBDM’s access on opportunity
* Central Marketing Manager – Role
* Impact of difference between RVP and RGM - None
* Access to Program Managers
  1. ***Assumptions***
* The job functions in Europe like BDM, RM will have the same access levels as identified in the document in the beginning. The job function does not change between various geographies although the regions that they handle might be more than the generic structure in US.

***General Exception across geography:***

One BU might be divided into more than one (as part of organizational realignment) and the verticals within the original BU are shifted across these BU. For e.g. Enterprise might be realigned as Enterprise and Enterprise Manufacturing. In this case, retail continues to lie within Enterprise and Manufacturing is realigned to Enterprise Manufacturing:

1. One new CRM BU can be created for Enterprise Manufacturing.
2. Administrator can then change the parent business unit for Manufacturing to Enterprise Manufacturing. This can be easily accomplished from the CRM Business Unit Administration screen.
3. All the data will accordingly be realigned
4. **Out of Scope Requirements**

Any item not covered in the identified scope of requirements will be considered as out-of-scope.