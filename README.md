**Muscle-Specific ECM Fibers Made with Anchored Cell Sheet Engineering Support Tissue Regeneration in Rat Models of Volumetric Muscle Loss**

**Overview:**

This computational pipeline was developed to analyze tissue regeneration in volumetric muscle loss (VML) treatment studies. It enables systematic quantification of tissue components and regenerative markers through automated image processing of histological and immunohistochemical slides. The pipeline supports objective assessment of spatial tissue heterogeneity, allowing researchers to track the progression of muscle regeneration, inflammatory responses, and tissue remodeling over time. By providing standardized analysis of tissue architecture and marker expression, this tool helps evaluate the efficacy of different therapeutic approaches in skeletal muscle regeneration.

**Features**

Automated processing of whole slide images (WSIs) with multiple staining types (H&E, Masson's Trichrome, Movat's Pentachrome, different IHC)
Region of Interest (ROI) detection and grid-based tiling
Color-based WSI segmentation
Quantification of observations in WSIs and defining Target Prevalence Index (TPI) for meaningful comparison of different conditions
Complex statistical analysis such as mixed-effects modeling
Comprehensive data visualization and reporting

**Content:**

<ins>Step 1:</ins> SVG files analyzed using QuPath are used as inputs. These files contain three layers including Original image, a contour line defining regions of interest (ROIs) comprising only the actively remodeling injury/treatment site, and grid lines. The code detects the three layers from each SVG file and saves them individually as high-resolution (300 dpi) PNG files.

<ins>Step 2:</ins> The code isolates each ROI from the entire slide using binary masks created from the ROI contour line, excluding surrounding regions. The masked regions are then subdivided into analysis tiles using the reference grid lines detected through adaptive thresholding and Hough transform techniques. Each tile maintains experimental traceability through a comprehensive naming convention incorporating treatment condition (Test, Sham, or Control), time point (Week 2, 4, or 8), staining type, and animal number. Empty tiles are excluded from analysis.

<ins>Step 3:</ins> Tissue components defined by the staining type are quantified using a color-based segmentation approach specific to each staining type. For histological stains, H&E (“Nuclei” dark purple, “Cytoplasm/Fibrosis/Muscle” pink/red, “Other” weakly stained), Masson’s Trichrome (“Nuclei/Cytoplasm” light red, “Fibrosis” blue/green, “Muscle” dark red, “Other” weakly stained), and Movat’s Pentachrome (“Nuclei/Elastic Fiber” dark brown/black, “Fibrosis” yellow/light brown, “Muscle/Cytoplasm” dark red, “Other” weakly stained) staining, tissue components are categorized based on their characteristic colors. For IHC targets (“Nuclei” purple, “Target” brown, “Other” weakly stained), the analysis distinguishes between DAB-positive regions (brown) and negative regions (purple counterstain). Segmentation is performed using a nearest-neighbor classification in RGB color space, with predefined color clusters established through comprehensive analysis of representative images. The area coverage for the stained part of each tile is measured, and the values are recorded for each condition at each time point. The distribution of tissue components across conditions and time points is visualized using box plots. For statistical analysis, a mixed-effects model is employed to account for both biological replicates (different animals) and technical replicates (multiple tiles per slide from each animal). 

<ins>Step 4:</ins> To provide temporal comparison between different conditions, an index called Target Prevalence Index (TPI) is defined for IHC targets by fitting a linear regression between the percentage of nuclei area from H&E staining as the independent variable and the percentage of IHC target-positive area as the dependent variable. TPI values are presented using box plots. For calculating TPI values, slide level data (mean of all tile values for each slide) are used with a weighted two-sample t-tests for statistical analysis purposes to account for standard deviations calculated from tile level data.

**Dependencies:** Google Colab environment, multiple Python libraries

**Contributions:** We welcome contributions to enhance this research. Please open issues for discussions or submit pull requests for code improvements.

**Credits:** This project aligns with Evolved.Bio's mission to advance regenerative medicine through anchored cell sheet engineering, machine learning, and biomanufacturing. As a Canadian biotechnology startup, Evolved.Bio pioneers innovative approaches to create a world-leading tissue foundry.

**License:** This work is published under [insert license details] as part of an open access publication [insert DOI].

**Contact:** For questions or collaborations, please reach out to Alireza Shahin (alireza@itsevolved.com).
